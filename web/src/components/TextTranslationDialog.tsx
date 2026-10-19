import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { errorMessage, translate, type Language } from "../i18n";
import type { TaskUsage } from "../types";
import { Modal } from "./Modal";
import { Feedback } from "./InteractionProvider";

type TextTranslationResult = {
  text: string;
  usage: TaskUsage | null;
  warnings: string[];
};

export function TextTranslationDialog({ project, language, open, onClose }: {
  project: string;
  language: Language;
  open: boolean;
  onClose: () => void;
}) {
  const t = (key: string) => translate(key, language);
  const [source, setSource] = useState("");
  const [result, setResult] = useState<TextTranslationResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState("");
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);

  async function translateText() {
    if (controller.current || !source.trim()) return;
    const request = new AbortController();
    controller.current = request;
    setBusy(true);
    setError(null);
    setNotice("");
    setResult(null);
    try {
      const next = await api<TextTranslationResult>(`/api/v1/projects/${encodeURIComponent(project)}/text-translation`, {
        method: "POST", body: JSON.stringify({ source }), signal: request.signal,
      });
      if (!request.signal.aborted) {
        setResult(next);
        setNotice(t("textTranslation.completed"));
      }
    } catch (reason) {
      if (request.signal.aborted) setNotice(t("textTranslation.cancelled"));
      else setError(reason);
    } finally {
      controller.current = null;
      setBusy(false);
    }
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(result!.text);
      setError(null);
      setNotice(t("textTranslation.copied"));
    } catch (reason) {
      setError(reason);
    }
  }

  function close() {
    controller.current?.abort();
    onClose();
  }

  if (!open) return null;
  return <Modal ariaLabel={t("textTranslation.title")} onClose={close} className="text-translation-dialog">
    <div className="text-translation-heading">
      <h2>{t("textTranslation.title")}</h2>
      <button className="quiet-button" onClick={close}>{t("textTranslation.close")}</button>
    </div>
    <p className="muted">{t("textTranslation.hint")}</p>
    <p className="muted">{t("textTranslation.termsHint")}</p>
    <div className="comparison">
      <label>{t("textTranslation.source")}
        <textarea autoFocus value={source} disabled={busy} onChange={(event) => {
          setSource(event.target.value); setResult(null); setNotice(""); setError(null);
        }} />
      </label>
      <label>{t("textTranslation.result")}
        <textarea readOnly value={result?.text ?? ""} placeholder={busy ? t("textTranslation.running") : ""} />
      </label>
    </div>
    {error != null && <Feedback message={errorMessage(error, language)} onDismiss={() => setError(null)} />}
    {result?.warnings.map((warning) => <Feedback message={warning} type="warning" key={warning} />)}
    {notice && <Feedback message={notice} type="success" onDismiss={() => setNotice("")} />}
    {result && <p className="muted">{result.usage?.available
      ? translate("repetition.usage", language, { input: result.usage.input_tokens, output: result.usage.output_tokens })
      : t("textTranslation.usageUnavailable")}{result.usage?.partial ? ` (${t("repetition.partialUsage")})` : ""}</p>}
    <div className="modal-actions">
      <button className="quiet-button" disabled={!result} onClick={() => void copy()}>{t("textTranslation.copy")}</button>
      {busy
        ? <button className="quiet-button" onClick={() => controller.current?.abort()}>{t("common.cancel")}</button>
        : <button className="primary-button" disabled={!source.trim()} onClick={() => void translateText()}>{t("textTranslation.translate")}</button>}
    </div>
  </Modal>;
}
