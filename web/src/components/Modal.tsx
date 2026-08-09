import type { ReactNode } from "react";

export function Modal({
  ariaLabel,
  children,
}: {
  ariaLabel: string;
  children: ReactNode;
}) {
  return (
    <div className="modal-backdrop">
      <div className="modal" role="dialog" aria-modal="true" aria-label={ariaLabel}>
        {children}
      </div>
    </div>
  );
}
