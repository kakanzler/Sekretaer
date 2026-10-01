"use client";

import { useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Badge, ErrorNotice } from "@/components/ui";
import type { ApiError } from "@/lib/errors";
import { formatDateTime, formatRange } from "@/lib/format";
import { CERTAINTY_LABEL } from "@/lib/labels";
import {
  addAction,
  addBullet,
  addTextItem,
  emptyNote,
  removeAction,
  removeBullet,
  removeDecision,
  removeTextItem,
  setCornellSummary,
  unconfirmedActions,
  updateAction,
  updateBulletText,
  updateDecision,
  updateTextItem,
  type TextListKey,
} from "@/lib/noteEdit";
import type { EvidencedText, Note, NoteRevision } from "@/lib/types";
import { EvidenceChips } from "./Evidence";
import { ActionCard, BulletTree, DecisionCard, EditableText } from "./NoteEditors";

type TabKey = "cornell" | "bullets" | "decisions" | "actions" | "questions";

const TABS: { key: TabKey; label: string }[] = [
  { key: "cornell", label: "Cornell" },
  { key: "bullets", label: "箇条書き" },
  { key: "decisions", label: "決定事項" },
  { key: "actions", label: "アクション" },
  { key: "questions", label: "未解決" },
];

function AddItem({ label, onAdd }: { label: string; onAdd: (text: string) => Promise<ApiError | null> }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const id = useId();
  return (
    <form
      className="row"
      onSubmit={async (e) => {
        e.preventDefault();
        if (!text.trim()) return;
        setBusy(true);
        const err = await onAdd(text.trim());
        setBusy(false);
        setError(err);
        if (!err) setText("");
      }}
    >
      <label htmlFor={id} className="visually-hidden">
        {label}
      </label>
      <input id={id} type="text" className="grow" placeholder={label} value={text} onChange={(e) => setText(e.target.value)} />
      <button type="submit" className="btn small" disabled={busy || !text.trim()}>
        追加
      </button>
      {error && (
        <div style={{ flexBasis: "100%" }}>
          <ErrorNotice error={error} title="追加できません" />
        </div>
      )}
    </form>
  );
}

function TextList({
  items,
  listKey,
  label,
  editable,
  save,
  note,
  withCertainty = false,
}: {
  items: (EvidencedText & { certainty?: keyof typeof CERTAINTY_LABEL })[];
  listKey: TextListKey;
  label: string;
  editable: boolean;
  save: (n: Note) => Promise<ApiError | null>;
  note: Note;
  withCertainty?: boolean;
}) {
  return (
    <>
      {items.length === 0 && <p className="muted small">項目はありません。</p>}
      <ul className="note-list">
        {items.map((it, i) => (
          <li key={`${i}-${it.text}`}>
            <div className="note-item">
              <EditableText
                value={it.text}
                label={label}
                editable={editable}
                onSave={(t) => save(updateTextItem(note, listKey, i, t))}
                onDelete={() => save(removeTextItem(note, listKey, i))}
              />
              <span className="row small">
                {withCertainty && it.certainty && (
                  <Badge tone={it.certainty === "stated" ? "neutral" : "warn"}>{CERTAINTY_LABEL[it.certainty]}</Badge>
                )}
                <EvidenceChips item={it} />
              </span>
            </div>
          </li>
        ))}
      </ul>
      {editable && <AddItem label={`${label}を追加`} onAdd={(t) => save(addTextItem(note, listKey, t))} />}
    </>
  );
}

export function NotePane({
  meetingId,
  revision,
  provisional,
  editable,
  onSave,
  controls,
  error,
}: {
  meetingId: string;
  revision: NoteRevision | null;
  provisional: boolean;
  editable: boolean;
  onSave: (note: Note) => Promise<ApiError | null>;
  controls?: ReactNode;
  error?: ApiError | null;
}) {
  const [tab, setTab] = useState<TabKey>("cornell");
  const tabRefs = useRef<Record<TabKey, HTMLButtonElement | null>>({ cornell: null, bullets: null, decisions: null, actions: null, questions: null });
  const baseId = useId();
  const note = revision?.note ?? null;
  const unconfirmed = unconfirmedActions(note);

  function onTabKey(e: KeyboardEvent<HTMLButtonElement>) {
    const idx = TABS.findIndex((t) => t.key === tab);
    let next = idx;
    if (e.key === "ArrowRight") next = (idx + 1) % TABS.length;
    else if (e.key === "ArrowLeft") next = (idx - 1 + TABS.length) % TABS.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = TABS.length - 1;
    else return;
    e.preventDefault();
    const key = TABS[next]!.key;
    setTab(key);
    tabRefs.current[key]?.focus();
  }

  function startManual() {
    return onSave(emptyNote(meetingId, new Date().toISOString()));
  }

  return (
    <section className="pane" aria-labelledby={`${baseId}-title`}>
      <div className="pane-head">
        <h2 id={`${baseId}-title`}>
          要約ノート{" "}
          {provisional && (
            <Badge tone="info">
              暫定（更新中）
            </Badge>
          )}
        </h2>
        {revision && (
          <span className="small muted">
            {revision.origin === "ai" ? "AI 生成" : "手動編集"}の版 / {formatDateTime(revision.createdAt)} / 範囲{" "}
            {formatRange(revision.note.coverage.fromMs, revision.note.coverage.toMs)}
          </span>
        )}
      </div>
      <p className="small muted">
        要約は逐語記録の代わりではなく、確認・修正するための作業メモです。
        {provisional && " 新しい要約を作成中のため、表示中の内容は更新される可能性があります。"}
      </p>
      {controls}
      <ErrorNotice error={error ?? null} title="ノートを取得できません" />

      {!note && (
        <div className="stack">
          <p className="muted">まだノートはありません。</p>
          {editable && (
            <button type="button" className="btn small" onClick={startManual}>
              手動でノートを作成
            </button>
          )}
        </div>
      )}

      {note && (
        <>
          <div role="tablist" aria-label="ノートの形式" className="tabs">
            {TABS.map((t) => (
              <button
                key={t.key}
                ref={(el) => {
                  tabRefs.current[t.key] = el;
                }}
                type="button"
                role="tab"
                id={`${baseId}-tab-${t.key}`}
                aria-selected={tab === t.key}
                aria-controls={`${baseId}-panel-${t.key}`}
                tabIndex={tab === t.key ? 0 : -1}
                className="tab"
                onClick={() => setTab(t.key)}
                onKeyDown={onTabKey}
              >
                {t.label}
                {t.key === "actions" && unconfirmed > 0 && <span className="small"> （未確認 {unconfirmed}）</span>}
                {t.key === "decisions" && note.decisions.length > 0 && <span className="small"> （{note.decisions.length}）</span>}
              </button>
            ))}
          </div>
          <div
            role="tabpanel"
            id={`${baseId}-panel-${tab}`}
            aria-labelledby={`${baseId}-tab-${tab}`}
            tabIndex={0}
            className="scroll"
          >
            {tab === "cornell" && (
              <div className="cornell">
                <div>
                  <h3>手がかり</h3>
                  <TextList items={note.cornell.cues} listKey="cues" label="手がかり" editable={editable} save={onSave} note={note} />
                </div>
                <div>
                  <h3>ノート</h3>
                  <TextList items={note.cornell.notes} listKey="notes" label="ノート" editable={editable} save={onSave} note={note} withCertainty />
                </div>
                <div className="cornell-summary">
                  <h3>要約</h3>
                  <EditableText
                    value={note.cornell.summary}
                    label="要約"
                    multiline
                    editable={editable}
                    onSave={(t) => onSave(setCornellSummary(note, t))}
                  />
                  {(note.cornell.sections ?? []).length > 0 && (
                    <>
                      <h3>区間ごとの要約</h3>
                      <ul className="note-list">
                        {note.cornell.sections!.map((s, i) => (
                          <li key={i}>
                            <span className="seg-time">{formatRange(s.fromMs, s.toMs)}</span> {s.summary}
                          </li>
                        ))}
                      </ul>
                    </>
                  )}
                </div>
              </div>
            )}
            {tab === "bullets" && (
              <>
                {note.bullets.length === 0 && <p className="muted small">項目はありません。</p>}
                <BulletTree
                  bullets={note.bullets}
                  editable={editable}
                  onSaveText={(id, t) => onSave(updateBulletText(note, id, t))}
                  onDelete={(id) => onSave(removeBullet(note, id))}
                />
                {editable && <AddItem label="箇条書きを追加" onAdd={(t) => onSave(addBullet(note, t))} />}
              </>
            )}
            {tab === "decisions" && (
              <>
                <p className="small muted">AI は合意と断定できない場合「要確認」にします。抽出の目安は確率ではありません。</p>
                {note.decisions.length === 0 && <p className="muted small">決定事項はありません。</p>}
                <ul style={{ listStyle: "none", padding: 0 }}>
                  {note.decisions.map((d) => (
                    <DecisionCard
                      key={d.id}
                      decision={d}
                      editable={editable}
                      onSave={(patch) => onSave(updateDecision(note, d.id, patch))}
                      onDelete={() => onSave(removeDecision(note, d.id))}
                    />
                  ))}
                </ul>
              </>
            )}
            {tab === "actions" && (
              <>
                <p className="small muted">AI が推定したアクションは「未確認」で作成されます。内容を確認して修正してください。</p>
                {note.actions.length === 0 && <p className="muted small">アクションはありません。</p>}
                <ul style={{ listStyle: "none", padding: 0 }}>
                  {note.actions.map((a) => (
                    <ActionCard
                      key={a.id}
                      action={a}
                      editable={editable}
                      onSave={(patch) => onSave(updateAction(note, a.id, patch))}
                      onDelete={() => onSave(removeAction(note, a.id))}
                    />
                  ))}
                </ul>
                {editable && <AddItem label="アクションを追加" onAdd={(t) => onSave(addAction(note, t))} />}
              </>
            )}
            {tab === "questions" && (
              <TextList items={note.openQuestions} listKey="openQuestions" label="未解決事項" editable={editable} save={onSave} note={note} />
            )}
          </div>
        </>
      )}
    </section>
  );
}
