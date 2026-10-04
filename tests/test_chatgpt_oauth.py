from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.chatgpt_oauth import ChatGPTConnection, ISSUER, RESOURCE
from app.errors import AppError


@pytest.fixture
def oauth_server(monkeypatch):
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    public.update(kid="test", alg="RS256", use="sig")
    calls = []
    options = {"nonce": "nonce", "subject": "user-1", "scope": "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"}

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": ISSUER + "/jwks", "revocation_endpoint": ISSUER + "/revoke"})
        if request.url.path == "/jwks":
            return httpx.Response(200, json={"keys": [public]})
        if request.url.path == "/revoke":
            return httpx.Response(200)
        form = parse_qs(request.content.decode())
        if options.get("refresh_error") and form["grant_type"] == ["refresh_token"]:
            return httpx.Response(400, json={"error": "invalid_grant"})
        token = jwt.encode({"iss": ISSUER, "aud": form["client_id"][0], "sub": options["subject"], "email": "test@example.com", "exp": time.time() + 3600, "nonce": options["nonce"]}, key, algorithm="RS256", headers={"kid": "test"})
        return httpx.Response(200, json={"access_token": "access-secret", "refresh_token": "refresh-secret", "id_token": token, "token_type": "Bearer", "expires_in": 3600, "scope": options["scope"]})

    original = httpx.AsyncClient
    monkeypatch.setattr("app.chatgpt_oauth.httpx.AsyncClient", lambda **kw: original(**kw, transport=httpx.MockTransport(handler)))
    return calls, options


def test_login_identity_permissions_and_secret_storage(oauth_server):
    async def run():
        connection = ChatGPTConnection()
        await connection.complete_login("code", "oaiapp_test", "nonce", "verifier", "http://127.0.0.1:1455/auth/callback", None)
        summary = connection.summary()
        assert summary["connected"] and summary["plan_enabled"]
        assert summary["email"] == "test@example.com"
        assert "secret" not in json.dumps(summary)
        assert "secret" not in connection.path.read_text()
        identity = connection.identity()
        assert await connection.access_token(identity) == "access-secret"
        await connection.logout()
        assert not connection.summary()["connected"]
        with pytest.raises(AppError):
            await connection.access_token(identity)
        assert connection.read()["registrations"]["oaiapp_test"]["subject"] == "user-1"
    asyncio.run(run())


@pytest.mark.parametrize("change", [{"nonce": "wrong"}, {"subject": "other"}])
def test_invalid_identity_does_not_replace_connection(oauth_server, change):
    async def run():
        connection = ChatGPTConnection()
        await connection.complete_login("code", "oaiapp_test", "nonce", "verifier", "http://127.0.0.1:1455/auth/callback", None)
        before = connection.identity()
        oauth_server[1].update(change)
        with pytest.raises(AppError):
            await connection.complete_login("code", "oaiapp_test", "nonce", "verifier", "http://127.0.0.1:1455/auth/callback", "oaiapp_test")
        assert connection.identity() == before
    asyncio.run(run())


def test_missing_plan_permission_blocks_inference(oauth_server):
    oauth_server[1]["scope"] = "openid profile email"
    async def run():
        connection = ChatGPTConnection()
        await connection.complete_login("code", "oaiapp_test", "nonce", "verifier", "http://127.0.0.1:1455/auth/callback", None)
        assert connection.summary()["connected"]
        assert not connection.summary()["plan_enabled"]
        with pytest.raises(AppError, match="授权"):
            await connection.access_token(connection.identity())
    asyncio.run(run())


def test_refresh_serialized_and_terminal_failure_clears_tokens(oauth_server):
    async def run():
        connection = ChatGPTConnection()
        await connection.complete_login("code", "oaiapp_test", "nonce", "verifier", "http://127.0.0.1:1455/auth/callback", None)
        identity = connection.identity()
        tokens = connection.tokens("oaiapp_test")
        tokens["expires_at"] = 0
        connection.save_tokens("oaiapp_test", tokens)
        assert await asyncio.gather(connection.access_token(identity), ChatGPTConnection().access_token(identity)) == ["access-secret"] * 2
        refreshes = [r for r in oauth_server[0] if b"grant_type=refresh_token" in r.content]
        assert len(refreshes) == 1
        assert connection.identity() == identity
        tokens = connection.tokens("oaiapp_test")
        tokens["expires_at"] = 0
        connection.save_tokens("oaiapp_test", tokens)
        oauth_server[1]["refresh_error"] = True
        with pytest.raises(AppError, match="登录"):
            await connection.access_token(identity)
        assert not connection.summary()["connected"]
    asyncio.run(run())


def test_loopback_state_cancel_and_browser_failure(monkeypatch):
    async def run():
        connection = ChatGPTConnection()
        urls = []
        monkeypatch.setattr("app.chatgpt_oauth.webbrowser.open", lambda url: urls.append(url) or True)
        await connection.login()
        query = parse_qs(urlsplit(urls[0]).query)
        assert query["client_id"] == ["dynamic_agent_client"]
        assert query["code_challenge_method"] == ["S256"]
        original = httpx.AsyncClient
        async with original(trust_env=False) as client:
            response = await client.get(query["redirect_uri"][0], params={"state": "wrong", "code": "secret"})
        assert response.status_code == 400
        assert "secret" not in response.text
        await connection.cancel()
        assert not connection.summary()["pending"]
        monkeypatch.setattr("app.chatgpt_oauth.webbrowser.open", lambda url: False)
        with pytest.raises(AppError, match="浏览器"):
            await connection.login()
        assert not connection.summary()["pending"]
    asyncio.run(run())
