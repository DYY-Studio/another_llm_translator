import { useEffect, useRef, type ReactNode } from "react";

export function Modal({ ariaLabel, children, onClose, className = "" }: {
  ariaLabel: string;
  children: ReactNode;
  onClose?: () => void;
  className?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const previousFocus = useRef(document.activeElement instanceof HTMLElement ? document.activeElement : null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const previous = previousFocus.current;
    const modal = ref.current!;
    if (!modal.contains(document.activeElement)) {
      (modal.querySelector<HTMLElement>("[autofocus], button, input, select, textarea, [tabindex='0']") ?? modal).focus();
    }
    function handleKey(event: KeyboardEvent) {
      const dialogs = document.querySelectorAll('[aria-modal="true"]');
      if (dialogs[dialogs.length - 1] !== modal) return;
      if (event.key === "Escape" && close.current) { event.preventDefault(); event.stopPropagation(); close.current(); }
      if (event.key !== "Tab") return;
      const controls = Array.from(modal.querySelectorAll<HTMLElement>('button, input, select, textarea, a[href], [tabindex="0"]'))
        .filter((node) => !node.matches(":disabled") && node.getClientRects().length > 0);
      const first = controls[0];
      const last = controls.at(-1);
      if (!first) { event.preventDefault(); modal.focus(); }
      else if (event.shiftKey && (document.activeElement === first || !modal.contains(document.activeElement))) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && (document.activeElement === last || !modal.contains(document.activeElement))) { event.preventDefault(); first.focus(); }
    }
    document.addEventListener("keydown", handleKey);
    return () => { document.removeEventListener("keydown", handleKey); if (previous?.isConnected) previous.focus(); };
  }, []);
  return <div className="modal-backdrop"><div ref={ref} className={`modal ${className}`} role="dialog" aria-modal="true" aria-label={ariaLabel} tabIndex={-1}>{children}</div></div>;
}
