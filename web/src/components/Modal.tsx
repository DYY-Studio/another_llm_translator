import type { ReactNode } from "react";

export function Modal({
  ariaLabel,
  children,
  className = "",
}: {
  ariaLabel: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className="modal-backdrop">
      <div className={`modal ${className}`} role="dialog" aria-modal="true" aria-label={ariaLabel}>
        {children}
      </div>
    </div>
  );
}
