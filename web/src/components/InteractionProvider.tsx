import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { detectLanguage, translate } from "../i18n";
import { Modal } from "./Modal";

type NoticeType = "success" | "error" | "warning";
const SUCCESS_NOTICE_DURATION = 5000;
type Notice = { id: number; text: string; type: NoticeType; onDismiss?: () => void };
type Question = { text: string; kind: "confirm" | "prompt" | "alert"; resolve: (value: string | null) => void };
const InteractionContext = createContext<{
  ask: (text: string, kind: Question["kind"]) => Promise<string | null>;
  notify: (text: string, type: NoticeType, onDismiss?: () => void) => void;
} | null>(null);

function useInteraction() {
  const context = useContext(InteractionContext);
  if (!context) throw new Error("InteractionProvider is required");
  return context;
}

export function useDialogs() {
  const { ask } = useInteraction();
  return {
    confirm: async (text: string) => (await ask(text, "confirm")) !== null,
    prompt: (text: string) => ask(text, "prompt"),
    alert: async (text: string) => { await ask(text, "alert"); },
  };
}

// Page-level results stay visible even when the initiating control has scrolled away.
export function Feedback({ message, type = "error", onDismiss }: { message: string; type?: NoticeType; onDismiss?: () => void }) {
  const { notify } = useInteraction();
  useEffect(() => { if (message) notify(message, type, onDismiss); }, [message, type, notify]);
  return null;
}

function NoticeItem({ notice, dismiss }: { notice: Notice; dismiss: (id: number) => void }) {
  useEffect(() => {
    if (notice.type !== "success") return;
    const timer = window.setTimeout(() => { dismiss(notice.id); notice.onDismiss?.(); }, SUCCESS_NOTICE_DURATION);
    return () => window.clearTimeout(timer);
  }, [notice.id, notice.type, dismiss]);
  return <div className={`feedback-notice feedback-${notice.type}`} role={notice.type === "error" ? "alert" : "status"}>
    <span>{notice.text}</span>
    <button type="button" className="quiet-button" aria-label={translate("common.dismiss", detectLanguage())} onClick={() => { dismiss(notice.id); notice.onDismiss?.(); }}>×</button>
    {notice.type === "success" && <div className="feedback-countdown" aria-hidden="true" style={{ animationDuration: `${SUCCESS_NOTICE_DURATION}ms` }} />}
  </div>;
}

export function InteractionProvider({ children }: { children: ReactNode }) {
  const [question, setQuestion] = useState<Question | null>(null);
  const [input, setInput] = useState("");
  const [notices, setNotices] = useState<Notice[]>([]);
  const pending = useRef<Question | null>(null);
  const nextId = useRef(0);
  const language = detectLanguage();
  const ask = useCallback((text: string, kind: Question["kind"]) => new Promise<string | null>((resolve) => {
    if (pending.current) throw new Error("A confirmation is already open");
    const next = { text, kind, resolve };
    pending.current = next;
    setInput("");
    setQuestion(next);
  }), []);
  const notify = useCallback((text: string, type: NoticeType, onDismiss?: () => void) => {
    const id = ++nextId.current;
    setNotices((current) => current.some((notice) => notice.text === text && notice.type === type)
      ? current : [...current, { id, text, type, onDismiss }]);
  }, []);
  const dismiss = useCallback((id: number) => setNotices((current) => current.filter((notice) => notice.id !== id)), []);
  function answer(value: string | null) {
    const current = pending.current;
    pending.current = null;
    setQuestion(null);
    current?.resolve(value);
  }
  useEffect(() => () => { pending.current?.resolve(null); pending.current = null; }, []);

  const context = useMemo(() => ({ ask, notify }), [ask, notify]);
  return <InteractionContext.Provider value={context}>
    {children}
    {question && createPortal(<div className="interaction-layer"><Modal ariaLabel={question.text} onClose={() => answer(null)}>
      <form onSubmit={(event) => { event.preventDefault(); answer(question.kind === "prompt" ? input.trim() : ""); }}>
        <p className="interaction-question">{question.text}</p>
        {question.kind === "prompt" && <input aria-label={question.text} autoFocus value={input} onChange={(event) => setInput(event.target.value)} />}
        <div className="modal-actions">
          {question.kind !== "alert" && <button type="button" className="quiet-button" autoFocus={question.kind === "confirm"} onClick={() => answer(null)}>{translate("common.cancel", language)}</button>}
          <button type="submit" className="primary-button" disabled={question.kind === "prompt" && !input.trim()}>{translate(question.kind === "alert" ? "common.dismiss" : "common.confirm", language)}</button>
        </div>
      </form>
    </Modal></div>, document.body)}
    {createPortal(<div className="feedback-stack" aria-label={language === "zh-CN" ? "操作反馈" : "Operation feedback"}>
      {notices.map((notice) => <NoticeItem key={notice.id} notice={notice} dismiss={dismiss} />)}
    </div>, document.body)}
  </InteractionContext.Provider>;
}
