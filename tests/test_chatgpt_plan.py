from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.chatgpt_oauth import ChatGPTConnection
from app.config import _resolve_llm_config, load_global_config
from app.errors import ConfigError, ExternalError, FatalExternalError
from app.llm_adapter import load_json_adapter
from app.llm_client import LLMClient, SlidingWindowLimiter
from app.llm_preset import load_llm_preset
from tests.test_chatgpt_oauth import oauth_server
from tests.test_llm_streaming import stream_response
from fastapi.testclient import TestClient
from app.web import create_app
from app.credentials import save_lan_password
from app.server_config import default_server_config

ROOT = Path(__file__).parents[1]


def plan_config(tmp_path):
    value = json.loads((ROOT / "llm_presets/openai-responses.json").read_text())
    value.update(adapter_id="chatgpt-plan", stream=True, proxy_url="", credential={"kind": "chatgpt", "name": "default"})
    path = tmp_path / "preset.json"
    path.write_text(json.dumps(value))
    return _resolve_llm_config(load_global_config(ROOT), adapter_file=ROOT / "llm_adapters/chatgpt-plan.json", preset=load_llm_preset(path))


def test_plan_contract_and_developer_messages(tmp_path):
    current = plan_config(tmp_path)
    headers, body = current["_llm_adapter"].build_request(api_key="test", model="chosen", messages=[{"role": "system", "content": "translate"}, {"role": "user", "content": "source"}], temperature=0.1, max_output_tokens=200, stream=True)
    assert body == {"model": "chosen", "input": [{"role": "developer", "content": "translate"}, {"role": "user", "content": "source"}], "store": False, "stream": True}
    assert headers == {"Authorization": "Bearer test"}
    with pytest.raises(ConfigError, match="temperature"):
        current["_llm_adapter"].build_request(api_key="test", model="chosen", messages=[], temperature=0, max_output_tokens=0, stream=True, extra_body={"temperature": 1})
    value = json.loads((tmp_path / "preset.json").read_text())
    for field, changed in [("stream", False), ("base_url", "https://example.com/v1"), ("credential", {"kind": "environment", "name": "KEY"})]:
        bad = {**value, field: changed}
        (tmp_path / "bad.json").write_text(json.dumps(bad))
        with pytest.raises(ConfigError):
            load_llm_preset(tmp_path / "bad.json")


@pytest.mark.asyncio
async def test_plan_inference_refreshes_each_attempt_without_changing_key_identity(tmp_path, oauth_server):
    connection = ChatGPTConnection()
    await connection.complete_login("code", "oaiapp_test", "nonce", "verifier", "http://127.0.0.1:1455/auth/callback", None)
    current = plan_config(tmp_path)
    seen = []
    def handler(request):
        seen.append(request)
        return stream_response({"type": "response.output_text.delta", "delta": "translation"}, {"type": "response.completed", "response": {"usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}})
    async with oauth_server[2](transport=httpx.MockTransport(handler)) as client:
        async with LLMClient(current, SlidingWindowLimiter(0, 0), run_dir=tmp_path / "run", project_id="PRJ", run_id="RUN", stage="translation", client=client) as llm:
            result, _ = await llm.chat(messages=[{"role": "user", "content": "source"}], temperature=0, estimated_input_tokens=10)
            assert result.content == "translation"
            key_ids = llm._key_ids
            tokens = connection.tokens("oaiapp_test")
            tokens["expires_at"] = 0
            connection.save_tokens("oaiapp_test", tokens)
            await llm.chat(messages=[{"role": "user", "content": "next"}], temperature=0, estimated_input_tokens=10)
            assert llm._key_ids == key_ids
            assert seen[0].url == "https://api.openai.com/v1/responses"
            assert all(r.headers["Authorization"] == "Bearer access-secret" for r in seen)
            await connection.logout()
            with pytest.raises(FatalExternalError):
                await llm.chat(messages=[], temperature=0, estimated_input_tokens=1)
    assert "access-secret" not in "".join(p.read_text() for p in (tmp_path / "run").rglob("*.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("in_stream", [False, True])
async def test_plan_usage_limit_is_fatal_without_retry(tmp_path, oauth_server, in_stream):
    connection = ChatGPTConnection()
    await connection.complete_login("code", "oaiapp_test", "nonce", "verifier", "http://127.0.0.1:1455/auth/callback", None)
    current = plan_config(tmp_path)
    current["retry"]["http_max_attempts"] = 3
    requests = []
    def handler(request):
        requests.append(request)
        error = {"code": "subscription_sharing_usage_limit_exceeded", "message": "limit"}
        return stream_response({"type": "error", **error}) if in_stream else httpx.Response(429, json={"error": error})
    async with oauth_server[2](transport=httpx.MockTransport(handler)) as client:
        async with LLMClient(current, SlidingWindowLimiter(0, 0), run_dir=tmp_path / "run", project_id="PRJ", run_id="RUN", stage="translation", client=client) as llm:
            with pytest.raises(FatalExternalError, match="管理用量"):
                await llm.chat(messages=[], temperature=0, estimated_input_tokens=1)
    assert len(requests) == 1


def test_models_and_lan_session_boundary(tmp_path, oauth_server):
    import asyncio
    import time
    connection = ChatGPTConnection()
    asyncio.run(connection.complete_login("code", "oaiapp_test", "nonce", "verifier", "http://127.0.0.1:1455/auth/callback", None))
    plan_config(tmp_path)
    definition = json.loads((tmp_path / "preset.json").read_text())
    config = default_server_config()
    config["lan"] = {"enabled": True, "bind_address": "0.0.0.0"}
    config["auth"]["username"] = "owner"
    save_lan_password("password")
    app = create_app(projects_root=tmp_path / "projects", server_config=config)
    from app.project import init_project
    from app.config import load_config, dump_config
    from app.user_config import user_root
    source = tmp_path / "source.txt"
    source.write_text("hello")
    root, _ = init_project([str(source)], name="demo", app_root=ROOT, projects_root=tmp_path / "projects")
    (user_root() / "llm_presets").mkdir(exist_ok=True)
    (user_root() / "llm_presets" / "openai-responses.json").write_text(json.dumps(definition))
    project_config = load_config(root / "config.toml")
    project_config["llm"]["preset"] = "openai-responses"
    (root / "config.toml").write_text(dump_config(project_config))
    with TestClient(app, client=("192.168.1.20", 12345)) as client:
        assert client.get("/api/v1/chatgpt/connection").status_code == 401
        assert client.post("/api/v1/global/presets/openai-responses/models", json=definition).status_code == 401
        assert client.get("/api/v1/projects/demo/task-options/translation").status_code == 401
        assert client.post("/api/v1/projects/demo/tasks", json={"stage": "translation"}).status_code == 401
        assert client.post("/api/v1/auth/login", json={"username": "owner", "password": "password"}).status_code == 200
        assert client.get("/api/v1/projects/demo/task-options/translation").json()["preset"] == {"id": "openai-responses", "model": definition["model"]}
        summary = client.get("/api/v1/chatgpt/connection").json()
        assert summary["connected"] and not summary["local"]
        assert client.post("/api/v1/chatgpt/connection/logout").status_code == 400
        definition["model"] = ""
        response = client.post("/api/v1/global/presets/openai-responses/models", json=definition)
        assert response.status_code == 200, response.text
        assert response.json()["models"] == [{"id": "visible", "display": "Visible model"}]


@pytest.mark.asyncio
async def test_plan_refresh_transient_errors_obey_retry_limit(tmp_path, oauth_server):
    from app.errors import ExternalError
    connection = ChatGPTConnection()
    await connection.complete_login("code", "oaiapp_test", "nonce", "verifier", "http://127.0.0.1:1455/auth/callback", None)
    tokens = connection.tokens("oaiapp_test")
    tokens["expires_at"] = 0
    connection.save_tokens("oaiapp_test", tokens)
    oauth_server[1]["refresh_transient"] = True
    current = plan_config(tmp_path)
    current["retry"].update(http_max_attempts=2, base_delay_seconds=0, jitter_seconds=0)
    async with oauth_server[2](transport=httpx.MockTransport(lambda r: pytest.fail("inference before refresh"))) as client:
        async with LLMClient(current, SlidingWindowLimiter(0, 0), run_dir=tmp_path / "run", project_id="PRJ", run_id="RUN", stage="translation", client=client) as llm:
            with pytest.raises(ExternalError, match="503"):
                await llm.chat(messages=[], temperature=0, estimated_input_tokens=1)
    assert len([r for r in oauth_server[0] if b"grant_type=refresh_token" in r.content]) == 2
    assert connection.summary()["connected"]


def test_plan_adapter_preview_uses_required_streaming(tmp_path):
    from fastapi.testclient import TestClient
    from app.web import create_app
    app = create_app(projects_root=tmp_path / "projects")
    with TestClient(app) as client:
        response = client.get("/api/v1/global/adapters/chatgpt-plan/preview")
        assert response.status_code == 200, response.text
        body = response.json()["body"]
        assert body["stream"] is True and body["store"] is False
        assert "temperature" not in body and "max_output_tokens" not in body
        response = client.get("/api/v1/global/adapters/openai-compatible/preview")
        assert response.status_code == 200, response.text
        assert response.json()["body"]["stream"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("content_type", [None, "application/json"])
async def test_plan_missing_content_type_warns_but_explicit_wrong_type_fails(tmp_path, oauth_server, content_type):
    connection = ChatGPTConnection()
    await connection.complete_login("code", "oaiapp_test", "nonce", "verifier", "http://127.0.0.1:1455/auth/callback", None)
    def handler(request):
        response = stream_response({"type": "response.output_text.delta", "delta": "OK"}, {"type": "response.completed"})
        del response.headers["content-type"]
        if content_type is not None:
            response.headers["content-type"] = content_type
        return response
    async with oauth_server[2](transport=httpx.MockTransport(handler)) as client:
        async with LLMClient(plan_config(tmp_path), SlidingWindowLimiter(0, 0), run_dir=tmp_path / "run", project_id="PRJ", run_id="RUN", stage="translation", client=client) as llm:
            if content_type is None:
                response, _ = await llm.chat(messages=[{"role": "user", "content": "test"}], temperature=0, estimated_input_tokens=1)
                assert response.content == "OK"
                assert any("未声明 Content-Type" in warning for warning in llm.warnings)
            else:
                with pytest.raises(ExternalError, match="HTTP 200，Content-Type: application/json"):
                    await llm.chat(messages=[], temperature=0, estimated_input_tokens=1)


def test_adapter_capabilities_follow_template_and_fixed_connection(tmp_path):
    plan = load_json_adapter(ROOT / "llm_adapters/chatgpt-plan.json")
    assert plan.capabilities == {
        "temperature": False, "max_output_tokens": False, "streaming": "required",
        "connection": {"base_url": "https://api.openai.com/v1", "credential": {"kind": "chatgpt", "name": "default"}, "proxy_source": "connection"},
    }
    regular = load_json_adapter(ROOT / "llm_adapters/openai-responses.json")
    assert regular.capabilities["streaming"] == "optional"
    assert regular.capabilities["max_output_tokens"] is True
    definition = json.loads((ROOT / "llm_adapters/openai-responses.json").read_text())
    definition["connection"] = {"base_url": "https://example.com/v1", "credential": {"kind": "environment", "name": "EXAMPLE_KEY"}, "proxy_source": "preset"}
    path = tmp_path / "adapter.json"
    path.write_text(json.dumps(definition))
    preset = json.loads((ROOT / "llm_presets/openai-responses.json").read_text())
    preset_path = tmp_path / "preset.json"
    preset_path.write_text(json.dumps(preset))
    with pytest.raises(ConfigError, match="固定连接"):
        _resolve_llm_config(load_global_config(ROOT), adapter_file=path, preset=load_llm_preset(preset_path))
    definition["body"]["stream"] = True
    path.write_text(json.dumps(definition))
    adapter = load_json_adapter(path)
    preset.update(base_url=definition["connection"]["base_url"], credential=definition["connection"]["credential"], stream=True)
    adapter.validate_preset(preset)
    preset["stream"] = False
    with pytest.raises(ConfigError, match="要求流式"):
        adapter.validate_preset(preset)
