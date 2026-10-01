"use client";

import { memo, useEffect, useId, useRef, useState } from "react";
import { useConnection } from "@/components/ConnectionProvider";
import { Dialog, ErrorNotice, Loading } from "@/components/ui";
import { prefersReducedMotion } from "@/lib/browser";
import { toApiError, type ApiError } from "@/lib/errors";
import { formatDateTime, formatMs, formatMsSpoken, plainText, splitForHighlight } from "@/lib/format";
import { SOURCE_LABEL } from "@/lib/labels";
import { countByFinality, filterLines, linesInOrder, type TranscriptLine, type TranscriptState } from "@/lib/transcript";
import type { Segment, SegmentHistoryItem } from "@/lib/types";

export interface HighlightRequest {
  id: string;
  n: number;
}

function Highlighted({ text, query }: { text: string; query: string }) {
  const parts = splitForHighlight(text, query);
  return (
    <>
      {parts.map((p, i) => (p.hit ? <mark key={i}>{p.text}</mark> : <span key={i}>{p.text}</span>))}
    </>
  );
}

const SegmentItem = memo(function SegmentItem({
  line,
  query,
  highlighted,
  editable,
  onSave,
  onHistory,
}: {
  line: TranscriptLine;
  query: string;
  highlighted: boolean;
  editable: boolean;
  onSave: (id: string, patch: { text: string; speakerLabel: string | null }) => Promise<ApiError | null>;
  onHistory: (id: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(line.text);
  const [speaker, setSpeaker] = useState(line.speakerLabel ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const textId = useId();
  const speakerId = useId();

  function beginEdit() {
    setText(line.text);
    setSpeaker(line.speakerLabel ?? "");
    setError(null);
    setEditing(true);
  }

  async function save() {
    setSaving(true);
    const err = await onSave(line.id, { text, speakerLabel: speaker.trim() || null });
    setSaving(false);
    setError(err);
    if (!err) setEditing(false);
  }

  const status = line.isFinal ? "確定" : "暫定";
  return (
    <li
      id={`seg-${line.id}`}
      tabIndex={-1}
      className={`seg ${line.isFinal ? "final" : "partial"}${highlighted ? " highlight" : ""}`}
      aria-label={`${formatMsSpoken(line.startMs)} ${SOURCE_LABEL[line.source]} ${status}`}
    >
      <div className="seg-meta">
        <span className="seg-time">{formatMs(line.startMs)}</span>
        <span>{SOURCE_LABEL[line.source] ?? line.source}</span>
        {line.speakerLabel && <strong>{line.speakerLabel}</strong>}
        <span className={`badge ${line.isFinal ? "neutral" : "info"}`}>{line.isFinal ? "確定" : "暫定（認識中）"}</span>
        {line.revision > 1 && <span className="badge warn">修正済み r{line.revision}</span>}
        {editable && line.isFinal && !editing && (
          <>
            <button type="button" className="btn ghost small" onClick={beginEdit}>
              修正
            </button>
            {line.revision > 1 && (
              <button type="button" className="btn ghost small" onClick={() => onHistory(line.id)}>
                訂正履歴
              </button>
            )}
          </>
        )}
      </div>
      {!editing ? (
        <p className="seg-text">
          <Highlighted text={plainText(line.text)} query={query} />
        </p>
      ) : (
        <div className="stack" style={{ marginTop: 6 }}>
          <div className="field">
            <label htmlFor={speakerId}>話者ラベル（任意）</label>
            <input id={speakerId} type="text" value={speaker} onChange={(e) => setSpeaker(e.target.value)} placeholder="例: 佐藤" />
          </div>
          <div className="field">
            <label htmlFor={textId}>発話テキスト</label>
            <textarea id={textId} value={text} onChange={(e) => setText(e.target.value)} />
          </div>
          <ErrorNotice error={error} title="修正を保存できません" />
          <div className="row">
            <button type="button" className="btn primary small" onClick={save} disabled={saving || !text.trim()}>
              {saving ? "保存中…" : "保存"}
            </button>
            <button type="button" className="btn small" onClick={() => setEditing(false)} disabled={saving}>
              キャンセル
            </button>
            <span className="small muted">元の認識結果は訂正履歴に残ります。</span>
          </div>
        </div>
      )}
    </li>
  );
});

function HistoryDialog({ meetingId, segmentId, onClose }: { meetingId: string; segmentId: string | null; onClose: () => void }) {
  const { client } = useConnection();
  const [items, setItems] = useState<{ id: string; list: SegmentHistoryItem[] } | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  useEffect(() => {
    if (!client || !segmentId) return;
    let cancelled = false;
    client.segmentHistory(meetingId, segmentId).then(
      (r) => {
        if (!cancelled) {
          setItems({ id: segmentId, list: r.items });
          setError(null);
        }
      },
      (e: unknown) => {
        if (!cancelled) setError(toApiError(e));
      },
    );
    return () => {
      cancelled = true;
    };
  }, [client, meetingId, segmentId]);
  const list = items && items.id === segmentId ? [...items.list].sort((a, b) => b.revision - a.revision) : null;
  return (
    <Dialog open={segmentId !== null} onClose={onClose} title="訂正履歴">
      <ErrorNotice error={error} />
      {!list && !error && <Loading />}
      {list && (
        <ol className="stack" style={{ listStyle: "none", padding: 0 }}>
          {list.map((h) => (
            <li key={h.revision} className="card">
              <div className="row small muted">
                <strong>r{h.revision}</strong>
                <span>{h.origin === "stt" ? "音声認識" : "手動修正"}</span>
                <span>{formatDateTime(h.editedAt)}</span>
                {h.speakerLabel && <span>話者: {h.speakerLabel}</span>}
              </div>
              <p className="seg-text">{plainText(h.text)}</p>
            </li>
          ))}
        </ol>
      )}
    </Dialog>
  );
}

export function TranscriptPane({
  meetingId,
  transcript,
  live,
  editable,
  highlight,
  onSegmentUpdated,
}: {
  meetingId: string;
  transcript: TranscriptState;
  live: boolean;
  editable: boolean;
  highlight: HighlightRequest | null;
  onSegmentUpdated: (s: Segment) => void;
}) {
  const { client } = useConnection();
  const [query, setQuery] = useState("");
  const [follow, setFollow] = useState(true);
  const [historyFor, setHistoryFor] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const searchId = useId();

  const all = linesInOrder(transcript);
  const lines = filterLines(all, query);
  const counts = countByFinality(all);
  const lastId = all.at(-1)?.id;
  const lastText = all.at(-1)?.text;

  // Follow the newest line while live, unless the user turned it off or is searching.
  useEffect(() => {
    if (!live || !follow || query) return;
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [live, follow, query, lastId, lastText]);

  // Evidence jump: clear the filter if it hides the target, then scroll + focus + highlight.
  const pendingJump = useRef<HighlightRequest | null>(null);
  useEffect(() => {
    if (!highlight) return;
    pendingJump.current = highlight;
    const t = setTimeout(() => {
      const req = pendingJump.current;
      if (!req) return;
      const el = document.getElementById(`seg-${req.id}`);
      if (!el) {
        setQuery("");
        return;
      }
      setFollow(false);
      el.scrollIntoView({ block: "center", behavior: prefersReducedMotion() ? "auto" : "smooth" });
      el.focus({ preventScroll: true });
      setFlash(req.id);
      pendingJump.current = null;
    }, 0);
    return () => clearTimeout(t);
  }, [highlight]);
  // Retry the jump after a filter reset re-rendered the list.
  useEffect(() => {
    const req = pendingJump.current;
    if (!req || query) return;
    const el = document.getElementById(`seg-${req.id}`);
    if (!el) return;
    el.scrollIntoView({ block: "center", behavior: prefersReducedMotion() ? "auto" : "smooth" });
    el.focus({ preventScroll: true });
    pendingJump.current = null;
    const t = setTimeout(() => setFlash(req.id), 0);
    return () => clearTimeout(t);
  }, [query]);
  useEffect(() => {
    if (!flash) return;
    const t = setTimeout(() => setFlash(null), 2500);
    return () => clearTimeout(t);
  }, [flash]);

  async function saveSegment(id: string, patch: { text: string; speakerLabel: string | null }): Promise<ApiError | null> {
    if (!client) return null;
    try {
      onSegmentUpdated(await client.patchSegment(meetingId, id, patch));
      return null;
    } catch (e) {
      return toApiError(e);
    }
  }

  return (
    <section className="pane" aria-labelledby="transcript-title">
      <div className="pane-head">
        <h2 id="transcript-title">逐語記録</h2>
        <span className="small muted" role="status">
          確定 {counts.final} 件{counts.partial > 0 ? ` / 暫定 ${counts.partial} 件` : ""}
        </span>
      </div>
      <div className="row" style={{ marginBottom: 8 }}>
        <label htmlFor={searchId} className="visually-hidden">
          逐語記録を検索
        </label>
        <input
          id={searchId}
          type="search"
          className="grow"
          placeholder="逐語記録を検索"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        {query && (
          <span className="small muted" role="status" aria-live="polite">
            {lines.length} 件一致
          </span>
        )}
        {live && (
          <label className="check small">
            <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} />
            最新に追従
          </label>
        )}
      </div>
      <p className="small muted">
        <span className="badge neutral">確定</span> は認識が確定した発話、<span className="badge info">暫定（認識中）</span>{" "}
        は認識途中で、確定時に置き換わります。
      </p>
      <div ref={scrollRef} className="scroll">
        {all.length === 0 && <p className="muted">{live ? "発話を待っています…" : "逐語記録はありません。"}</p>}
        <ol className="transcript">
          {lines.map((l) => (
            <SegmentItem
              key={l.id}
              line={l}
              query={query}
              highlighted={flash === l.id}
              editable={editable}
              onSave={saveSegment}
              onHistory={setHistoryFor}
            />
          ))}
        </ol>
      </div>
      <HistoryDialog meetingId={meetingId} segmentId={historyFor} onClose={() => setHistoryFor(null)} />
    </section>
  );
}
