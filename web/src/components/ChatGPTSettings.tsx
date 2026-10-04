import { useEffect, useState } from "react";
import { api } from "../api";
import { errorMessage, translate, type Language } from "../i18n";
import { openExternalUrl } from "../native";

export interface ChatGPTConnectionSummary {
  connected: boolean; can_switch_account: boolean; plan_enabled: boolean; pending: boolean; email: string;
  proxy_url: string; error: string; local: boolean; welcome_required: boolean;
}

interface UsageWindow { used_percent: number; window_minutes: number | null; reset_at: number | null }
interface UsageObservation { observed_at: number; http_status: number; errors: string[]; buckets: Record<string, Record<string, UsageWindow>> }
type UsageSnapshot = Record<string, UsageObservation>;

export function PlanUsageLink({ language }: { language: Language }) {
  const [error, setError] = useState("");
  return <><button type="button" className="quiet-button" onClick={() => {
    setError("");
    void openExternalUrl("https://chatgpt.com/#settings/Usage").catch((reason) => setError(errorMessage(reason, language)));
  }}>{translate("chatgpt.manageUsage", language)}</button>{error && <small role="alert">{error}</small>}</>;
}

export function ChatGPTSettings({ language }: { language: Language }) {
  const [connection, setConnection] = useState<ChatGPTConnectionSummary | null>(null);
  const [usage, setUsage] = useState<UsageSnapshot>({});
  const [proxy, setProxy] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    let active = true;
    void api<ChatGPTConnectionSummary>("/api/v1/chatgpt/connection").then((value) => {
      if (active) { setConnection(value); setProxy(value.proxy_url); }
    }).catch((reason) => { if (active) setError(errorMessage(reason, language)); });
    return () => { active = false; };
  }, []);
  useEffect(() => {
    if (!connection?.pending) return;
    let active = true;
    const timer = window.setTimeout(() => {
      void api<ChatGPTConnectionSummary>("/api/v1/chatgpt/connection").then((value) => {
        if (active) setConnection(value);
      }).catch((reason) => { if (active) setError(errorMessage(reason, language)); });
    }, 1000);
    return () => { active = false; window.clearTimeout(timer); };
  }, [connection]);
  async function action(name: string) {
    setBusy(true); setError(""); setSaved(false); setUsage({});
    try { setConnection(await api<ChatGPTConnectionSummary>(`/api/v1/chatgpt/connection/${name}`, { method: "POST" })); }
    catch (reason) { setError(errorMessage(reason, language)); }
    finally { setBusy(false); }
  }
  async function loadUsage(query: boolean) {
    setBusy(true); setError("");
    try { setUsage(await api<UsageSnapshot>("/api/v1/chatgpt/usage", { method: query ? "POST" : "GET" })); }
    catch (reason) { setError(errorMessage(reason, language)); }
    finally { setBusy(false); }
  }
  async function saveProxy() {
    setBusy(true); setError("");
    try {
      const value = await api<ChatGPTConnectionSummary>("/api/v1/chatgpt/connection", { method: "PUT", body: JSON.stringify({ proxy_url: proxy }) });
      setConnection(value); setProxy(value.proxy_url); setSaved(true);
    } catch (reason) { setError(errorMessage(reason, language)); }
    finally { setBusy(false); }
  }
  return <div className="config-settings">
    <div className="page-heading config-heading settings-action-heading"><div><h1>ChatGPT Plan</h1><p>{translate("chatgpt.subtitle", language)}</p></div><div className="button-group"><PlanUsageLink language={language} />{connection?.local && <button className="primary-button" disabled={busy || connection.pending} onClick={() => void saveProxy()}>{translate("common.save", language)}</button>}</div></div>
    {(error || connection?.error) && <div className="error-banner" role="alert">{error || connection?.error}</div>}
    {saved && <p className="success-text">{translate("chatgpt.saved", language)}</p>}
    {connection && <div className="config-form"><section className="config-section">
      <h2>{translate("chatgpt.connection", language)}</h2><p>{translate(connection.pending ? "chatgpt.pending" : connection.connected ? connection.plan_enabled ? "chatgpt.ready" : "chatgpt.notGranted" : "chatgpt.disconnected", language)}{connection.email && ` · ${connection.email}`}</p>
      {!connection.local && <p className="muted">{translate("chatgpt.localOnly", language)}</p>}
      <div className="config-grid"><label className="config-field"><span>{translate("preset.proxyUrl", language)}</span><input value={proxy} disabled={!connection.local || busy || connection.pending} onChange={(event) => { setProxy(event.target.value); setSaved(false); }} /><small>{translate("chatgpt.proxyHint", language)}</small></label></div>
      {connection.local && <div className="button-group">
        {connection.pending ? <button className="quiet-button" disabled={busy} onClick={() => void action("cancel")}>{translate("common.cancel", language)}</button> : <button className="primary-button" disabled={busy || proxy !== connection.proxy_url} onClick={() => void action("login")}>Continue with ChatGPT</button>}
        {connection.email && <button className="danger-button" disabled={busy || connection.pending} onClick={() => void action("logout")}>{translate("chatgpt.logout", language)}</button>}
        {connection.can_switch_account && <button className="quiet-button" disabled={busy || connection.pending || proxy !== connection.proxy_url} onClick={() => void action("login-new")}>{translate("chatgpt.switch", language)}</button>}
      </div>}
      </section>
      <section className="config-section">
        <h2>{translate("chatgpt.usageExperiment", language)}</h2>
        <p>{translate("chatgpt.usageExperimentHint", language)}</p>
        <div className="button-group">
          {connection.local && <button className="quiet-button" disabled={busy || !connection.plan_enabled} onClick={() => void loadUsage(true)}>{translate("chatgpt.queryUsage", language)}</button>}
          <button className="quiet-button" disabled={busy || !connection.plan_enabled} onClick={() => void loadUsage(false)}>{translate("chatgpt.readUsageHeaders", language)}</button>
        </div>
        {Object.keys(usage).length === 0 && <p className="muted">{translate("chatgpt.noUsage", language)}</p>}
        {Object.entries(usage).map(([source, observation]) => <div key={source}>
          <h3>{source === "endpoint" ? "wham/usage" : "Response headers"}</h3>
          <p className="muted">{new Date(observation.observed_at * 1000).toLocaleString(language)} · HTTP {observation.http_status}</p>
          {observation.errors.map((message) => <p key={message} role="alert">{message}</p>)}
          {Object.keys(observation.buckets).length === 0 && <p>{translate("chatgpt.noUsage", language)}</p>}
          {Object.entries(observation.buckets).flatMap(([bucket, windows]) => Object.entries(windows).map(([name, window]) => <p key={`${bucket}/${name}`}>
            {bucket} / {name} · {translate("chatgpt.remaining", language)} {(100 - window.used_percent).toFixed(1)}%
            {window.window_minutes !== null && ` · ${window.window_minutes} min`}
            {window.reset_at !== null && ` · ${translate("chatgpt.resetAt", language)} ${new Date(window.reset_at * 1000).toLocaleString(language)}`}
          </p>))}
        </div>)}
      </section>
      {connection.local && connection.welcome_required && <div className="modal-backdrop"><section className="modal" role="dialog" aria-modal="true" aria-label="ChatGPT Plan"><h2>ChatGPT Plan</h2><p>{translate("chatgpt.welcome", language)}</p><div className="button-group"><PlanUsageLink language={language} /><button className="primary-button" disabled={busy} onClick={() => void action("welcome-dismiss")}>{translate("chatgpt.gotIt", language)}</button></div></section></div>}
    </div>}
  </div>;
}
