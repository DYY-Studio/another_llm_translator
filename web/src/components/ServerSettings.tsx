import { useEffect, useState, type FormEvent } from "react";
import { api } from "../api";
import { translate, type Language } from "../i18n";
import type { InterfaceEntry, ServerStatus } from "../types";

function errorMessage(reason: unknown, language: Language): string { return reason instanceof Error ? reason.message : translate("common.requestFailed", language); }

export function LoginView({ language, onLoggedIn }: { language: Language; onLoggedIn: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      await api("/api/v1/auth/login", { method: "POST", body: JSON.stringify({ username, password }) });
      onLoggedIn();
    } catch (reason) {
      setError(errorMessage(reason, language));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="login-page">
      <form className="login-card" onSubmit={(event) => void submit(event)}>
        <h1>{translate("brand", language)}</h1>
        <p className="muted">{translate("login.subtitle", language)}</p>
        <label className="config-field">{translate("login.username", language)}<input value={username} autoComplete="username" onChange={(event) => setUsername(event.target.value)} /></label>
        <label className="config-field">{translate("login.password", language)}<input type="password" value={password} autoComplete="current-password" onChange={(event) => setPassword(event.target.value)} /></label>
        {error && <div className="error-banner">{error}</div>}
        <button className="primary-button" disabled={submitting || !username || !password}>{submitting ? translate("login.submitting", language) : translate("login.submit", language)}</button>
      </form>
    </div>
  );
}

export function ServerSettings({ language, onChanged }: { language: Language; onChanged: () => void }) {
  const [status, setStatus] = useState<ServerStatus | null>(null);
  const [interfaces, setInterfaces] = useState<InterfaceEntry[]>([]);
  const [enabled, setEnabled] = useState(false);
  const [bindAddress, setBindAddress] = useState("");
  const [required, setRequired] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  async function load() {
    const [statusResponse, interfaceResponse] = await Promise.all([
      api<ServerStatus>("/api/v1/server/status"),
      api<{ interfaces: InterfaceEntry[] }>("/api/v1/server/interfaces"),
    ]);
    setStatus(statusResponse);
    setInterfaces(interfaceResponse.interfaces);
    setEnabled(statusResponse.lan.enabled);
    setBindAddress(statusResponse.lan.bind_address);
    setRequired(statusResponse.auth.required);
    setUsername(statusResponse.auth.username);
    setPassword("");
  }

  useEffect(() => {
    void load().catch((reason) => setError(errorMessage(reason, language)));
  }, []);

  async function save() {
    const enablingWithoutAuth = enabled && !required;
    if (
      enablingWithoutAuth &&
      !window.confirm(translate("server.warningConfirm", language))
    ) {
      return;
    }
    setSaving(true);
    setMessage("");
    setError("");
    try {
      const result = await api<{ warning: string }>("/api/v1/server/config", {
        method: "PUT",
        body: JSON.stringify({
          lan: { enabled, bind_address: enabled ? bindAddress : "" },
          auth: { required, username, password },
        }),
      });
      setMessage(
        result.warning
          ? translate("server.warningEnabled", language)
          : translate("server.saved", language),
      );
      onChanged();
      await load();
    } catch (reason) {
      setError(errorMessage(reason, language));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="config-settings">
      <div className="page-heading config-heading settings-action-heading">
        <div><h1>{translate("server.title", language)}</h1><p>{translate("server.subtitle", language)}</p></div>
        <div className="button-group"><button className="primary-button" disabled={saving || (enabled && !bindAddress)} onClick={() => void save()}>{saving ? translate("common.saving", language) : translate("common.validateSave", language)}</button></div>
      </div>
      {error && <div className="error-banner">{error}</div>}
      {message && <p className="success-text">{message}</p>}
      {status && !status.loopback && <div className="warning-banner">{translate("server.lanViewHint", language)}</div>}
      <div className="config-form">
        <section className="config-section">
          <h2>{translate("server.sharing", language)}</h2>
          <div className="config-grid">
            <label className="config-toggle"><span><input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />{translate("server.enable", language)}</span><small>{translate("server.enableHint", language)}</small></label>
            {enabled && (
              <label className="config-field">{translate("server.interface", language)}
                <select value={bindAddress} onChange={(event) => setBindAddress(event.target.value)}>
                  <option value="">{translate("server.selectInterface", language)}</option>
                  {interfaces.map((item) => <option key={item.address} value={item.address}>{item.name} · {item.address}</option>)}
                </select>
              </label>
            )}
          </div>
        </section>
        {enabled && (
          <section className="config-section">
            <h2>{translate("server.auth", language)}</h2>
            <div className="config-grid">
              <label className="config-toggle"><span><input type="checkbox" checked={required} onChange={(event) => setRequired(event.target.checked)} />{translate("server.authRequired", language)}</span><small>{translate("server.authHint", language)}</small></label>
              {required && (
                <>
                  <label className="config-field">{translate("server.username", language)}<input value={username} autoComplete="off" onChange={(event) => setUsername(event.target.value)} /></label>
                  <label className="config-field">{translate("server.password", language)}<input type="password" value={password} autoComplete="new-password" placeholder={translate("server.passwordKeep", language)} onChange={(event) => setPassword(event.target.value)} /></label>
                </>
              )}
            </div>
          </section>
        )}
      </div>
    </div>
  );
}
