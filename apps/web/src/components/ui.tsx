"use client";

// Shared UI primitives: modal dialog, error notice, status badges.

import { useEffect, useId, useRef, type ReactNode } from "react";
import type { ApiError } from "@/lib/errors";

export function Dialog({
  open,
  onClose,
  title,
  children,
  footer,
  wide = false,
  describedBy,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
  describedBy?: string;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);
  return (
    <dialog
      ref={ref}
      className={`dialog${wide ? " wide" : ""}`}
      aria-labelledby={titleId}
      aria-describedby={describedBy}
      onClose={() => {
        if (open) onClose();
      }}
    >
      <div className="dialog-head">
        <h2 id={titleId}>{title}</h2>
        <button type="button" className="btn ghost" onClick={onClose} aria-label="閉じる">
          ×
        </button>
      </div>
      <div className="dialog-body">{open ? children : null}</div>
      {footer && <div className="dialog-foot">{footer}</div>}
    </dialog>
  );
}

/** Error display per spec §9: user-facing explanation separated from the diagnostic code. */
export function ErrorNotice({
  error,
  title,
  children,
}: {
  error: ApiError | null;
  title?: string;
  children?: ReactNode;
}) {
  if (!error) return null;
  return (
    <div className="notice danger" role="alert">
      {title && <strong className="notice-title">{title}</strong>}
      <p>{error.userMessage}</p>
      <p className="diag">
        診断コード: <code>{error.code}</code>
        {error.requestId && (
          <>
            {" "}
            / requestId: <code>{error.requestId}</code>
          </>
        )}
      </p>
      {children}
    </div>
  );
}

export function Badge({ tone = "neutral", children }: { tone?: "neutral" | "ok" | "warn" | "danger" | "info" | "rec"; children: ReactNode }) {
  return <span className={`badge ${tone}`}>{children}</span>;
}

export function Loading({ label = "読み込み中…" }: { label?: string }) {
  return (
    <p className="muted" role="status" aria-live="polite">
      {label}
    </p>
  );
}
