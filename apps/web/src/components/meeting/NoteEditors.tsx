"use client";

import { useId, useState } from "react";
import { Badge, ErrorNotice } from "@/components/ui";
import type { ApiError } from "@/lib/errors";
import { formatCertainty, formatMs, plainText } from "@/lib/format";
import { ACTION_STATUS_LABEL, CERTAINTY_LABEL, DECISION_STATUS_LABEL } from "@/lib/labels";
import type { ActionItem, ActionStatus, Bullet, Decision, DecisionStatus } from "@/lib/types";
import { EvidenceChips } from "./Evidence";

export type SaveFn = () => Promise<ApiError | null>;

/** Text with an inline edit mode. `onSave` returns an error or null. */
export function EditableText({
  value,
  label,
  editable,
  multiline = false,
  onSave,
  onDelete,
}: {
  value: string;
  label: string;
  editable: boolean;
  multiline?: boolean;
  onSave: (text: string) => Promise<ApiError | null>;
  onDelete?: () => Promise<ApiError | null>;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const id = useId();

  async function run(fn: () => Promise<ApiError | null>) {
    setBusy(true);
    const err = await fn();
    setBusy(false);
    setError(err);
    if (!err) setEditing(false);
  }

  if (!editing) {
    return (
      <span className="row" style={{ alignItems: "baseline" }}>
        <span className="grow" style={{ whiteSpace: "pre-wrap" }}>
          {plainText(value) || <span className="muted">（空）</span>}
        </span>
        {editable && (
          <button
            type="button"
            className="btn ghost small"
            onClick={() => {
              setDraft(value);
              setError(null);
              setEditing(true);
            }}
            aria-label={`${label}を編集`}
          >
            編集
          </button>
        )}
      </span>
    );
  }
  return (
    <span className="stack" style={{ display: "block" }}>
      <label htmlFor={id} className="visually-hidden">
        {label}
      </label>
      {multiline ? (
        <textarea id={id} value={draft} onChange={(e) => setDraft(e.target.value)} />
      ) : (
        <input id={id} type="text" value={draft} onChange={(e) => setDraft(e.target.value)} style={{ width: "100%" }} />
      )}
      <ErrorNotice error={error} title="保存できません" />
      <span className="row">
        <button type="button" className="btn primary small" disabled={busy || (!multiline && !draft.trim())} onClick={() => run(() => onSave(draft))}>
          保存
        </button>
        <button type="button" className="btn small" disabled={busy} onClick={() => setEditing(false)}>
          キャンセル
        </button>
        {onDelete && (
          <button type="button" className="btn small" disabled={busy} onClick={() => run(onDelete)}>
            削除
          </button>
        )}
      </span>
    </span>
  );
}

export function BulletTree({
  bullets,
  editable,
  onSaveText,
  onDelete,
}: {
  bullets: Bullet[];
  editable: boolean;
  onSaveText: (id: string, text: string) => Promise<ApiError | null>;
  onDelete: (id: string) => Promise<ApiError | null>;
}) {
  if (bullets.length === 0) return null;
  return (
    <ul className="note-list">
      {bullets.map((b) => (
        <li key={b.id}>
          <div className="note-item">
            <EditableText
              value={b.text}
              label="箇条書き項目"
              editable={editable}
              onSave={(t) => onSaveText(b.id, t)}
              onDelete={() => onDelete(b.id)}
            />
            <span className="row small">
              {b.certainty && <Badge tone={b.certainty === "stated" ? "neutral" : "warn"}>{CERTAINTY_LABEL[b.certainty]}</Badge>}
              <EvidenceChips item={b} />
            </span>
          </div>
          <BulletTree bullets={b.children ?? []} editable={editable} onSaveText={onSaveText} onDelete={onDelete} />
        </li>
      ))}
    </ul>
  );
}

export function DecisionCard({
  decision,
  editable,
  onSave,
  onDelete,
}: {
  decision: Decision;
  editable: boolean;
  onSave: (patch: Partial<Decision>) => Promise<ApiError | null>;
  onDelete: () => Promise<ApiError | null>;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const statusId = useId();

  async function run(fn: () => Promise<ApiError | null>) {
    setBusy(true);
    setError(await fn());
    setBusy(false);
  }

  return (
    <li className={`card${decision.status === "needs_review" ? " unconfirmed" : ""}`}>
      <div className="row small">
        <Badge tone={decision.status === "agreed" ? "ok" : decision.status === "needs_review" ? "warn" : "neutral"}>
          {DECISION_STATUS_LABEL[decision.status]}
        </Badge>
        {decision.atMs !== undefined && <span className="seg-time">{formatMs(decision.atMs)}</span>}
        <span className="muted" title="モデル由来の目安であり、確率ではありません">
          抽出{formatCertainty(decision.certainty)}
        </span>
      </div>
      <EditableText value={decision.text} label="決定事項" editable={editable} onSave={(text) => onSave({ text })} onDelete={onDelete} />
      <div className="row small">
        <EvidenceChips item={decision} />
        {editable && (
          <>
            <label htmlFor={statusId} className="muted">
              状態
            </label>
            <select
              id={statusId}
              value={decision.status}
              disabled={busy}
              onChange={(e) => run(() => onSave({ status: e.target.value as DecisionStatus }))}
            >
              {(Object.keys(DECISION_STATUS_LABEL) as DecisionStatus[]).map((s) => (
                <option key={s} value={s}>
                  {DECISION_STATUS_LABEL[s]}
                </option>
              ))}
            </select>
          </>
        )}
      </div>
      <ErrorNotice error={error} title="保存できません" />
    </li>
  );
}

export function ActionCard({
  action,
  editable,
  onSave,
  onDelete,
}: {
  action: ActionItem;
  editable: boolean;
  onSave: (next: Partial<ActionItem>) => Promise<ApiError | null>;
  onDelete: () => Promise<ApiError | null>;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(action);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const ids = { text: useId(), assignee: useId(), due: useId(), status: useId() };

  async function run(fn: () => Promise<ApiError | null>, closeOnOk = false) {
    setBusy(true);
    const err = await fn();
    setBusy(false);
    setError(err);
    if (!err && closeOnOk) setEditing(false);
  }

  const aiLabel = action.origin === "user" ? "手動" : "AI 推定";
  return (
    <li className={`card${action.confirmed ? "" : " unconfirmed"}`}>
      <div className="row small">
        {action.confirmed ? <Badge tone="ok">確認済み</Badge> : <Badge tone="warn">未確認</Badge>}
        <Badge tone="neutral">{aiLabel}</Badge>
        <Badge tone={action.status === "done" ? "ok" : "neutral"}>{ACTION_STATUS_LABEL[action.status]}</Badge>
      </div>
      {!editing ? (
        <>
          <p style={{ whiteSpace: "pre-wrap" }}>{plainText(action.text)}</p>
          <dl className="kv small">
            <dt>担当</dt>
            <dd>{action.assignee ? plainText(action.assignee) : <span className="muted">未定</span>}</dd>
            <dt>期限</dt>
            <dd>{action.dueDate ? plainText(action.dueDate) : <span className="muted">未定</span>}</dd>
            <dt>根拠</dt>
            <dd>
              <EvidenceChips item={action} />
            </dd>
          </dl>
          {editable && (
            <div className="row">
              {!action.confirmed && (
                <button type="button" className="btn small primary" disabled={busy} onClick={() => run(() => onSave({ confirmed: true }))}>
                  内容を確認済みにする
                </button>
              )}
              <button
                type="button"
                className="btn small"
                disabled={busy}
                onClick={() => {
                  setDraft(action);
                  setError(null);
                  setEditing(true);
                }}
              >
                編集
              </button>
            </div>
          )}
        </>
      ) : (
        <div className="stack">
          <div className="field">
            <label htmlFor={ids.text}>内容</label>
            <textarea id={ids.text} value={draft.text} onChange={(e) => setDraft({ ...draft, text: e.target.value })} />
          </div>
          <div className="grid-2">
            <div className="field">
              <label htmlFor={ids.assignee}>担当者</label>
              <input
                id={ids.assignee}
                type="text"
                value={draft.assignee ?? ""}
                placeholder="未定"
                onChange={(e) => setDraft({ ...draft, assignee: e.target.value || null })}
              />
            </div>
            <div className="field">
              <label htmlFor={ids.due}>期限</label>
              <input
                id={ids.due}
                type="text"
                value={draft.dueDate ?? ""}
                placeholder="未定（例: 来週金曜）"
                onChange={(e) => setDraft({ ...draft, dueDate: e.target.value || null })}
              />
              <span className="hint">発言どおりの表現で記録します（自動で日付に変換しません）。</span>
            </div>
            <div className="field">
              <label htmlFor={ids.status}>状態</label>
              <select id={ids.status} value={draft.status} onChange={(e) => setDraft({ ...draft, status: e.target.value as ActionStatus })}>
                {(Object.keys(ACTION_STATUS_LABEL) as ActionStatus[]).map((s) => (
                  <option key={s} value={s}>
                    {ACTION_STATUS_LABEL[s]}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <label className="check">
            <input type="checkbox" checked={draft.confirmed} onChange={(e) => setDraft({ ...draft, confirmed: e.target.checked })} />
            内容を確認済みにする
          </label>
          <div className="row">
            <button
              type="button"
              className="btn primary small"
              disabled={busy || !draft.text.trim()}
              onClick={() =>
                run(
                  () =>
                    onSave({
                      text: draft.text,
                      assignee: draft.assignee?.trim() || null,
                      dueDate: draft.dueDate?.trim() || null,
                      status: draft.status,
                      confirmed: draft.confirmed,
                    }),
                  true,
                )
              }
            >
              保存
            </button>
            <button type="button" className="btn small" disabled={busy} onClick={() => setEditing(false)}>
              キャンセル
            </button>
            <button type="button" className="btn small" disabled={busy} onClick={() => run(onDelete, true)}>
              削除
            </button>
          </div>
        </div>
      )}
      <ErrorNotice error={error} title="保存できません" />
    </li>
  );
}
