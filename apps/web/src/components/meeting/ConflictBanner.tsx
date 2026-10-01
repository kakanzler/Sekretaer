"use client";

import { useState } from "react";
import { useConnection } from "@/components/ConnectionProvider";
import { ErrorNotice } from "@/components/ui";
import { toApiError, type ApiError } from "@/lib/errors";
import { formatDateTime } from "@/lib/format";
import { diffNotes } from "@/lib/noteDiff";
import type { NoteRevision } from "@/lib/types";
import { DiffView } from "./DiffView";

/**
 * AC-06: a later AI summary never silently overwrites a manually edited note.
 * The pending AI revision is shown side by side and the user chooses.
 */
export function ConflictBanner({
  meetingId,
  current,
  conflict,
  onResolved,
}: {
  meetingId: string;
  current: NoteRevision | null;
  conflict: NoteRevision;
  onResolved: (rev: NoteRevision) => void;
}) {
  const { client } = useConnection();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [expanded, setExpanded] = useState(true);
  const entries = current ? diffNotes(current.note, conflict.note) : [];

  async function resolve(action: "accept_ai" | "keep_mine") {
    if (!client) return;
    setBusy(true);
    setError(null);
    try {
      onResolved(await client.resolveConflict(meetingId, action, conflict.id));
    } catch (e) {
      setError(toApiError(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="notice warn stack" role="region" aria-labelledby="conflict-title">
      <h2 id="conflict-title">要約の更新が手修正と競合しています</h2>
      <p>
        手修正したノートの後に AI の新しい要約（{formatDateTime(conflict.createdAt)}）が届きました。自動では上書きしていません。
        違いを確認して、どちらの版を使うか選んでください。
      </p>
      <button type="button" className="btn small" aria-expanded={expanded} onClick={() => setExpanded((v) => !v)}>
        {expanded ? "差分を隠す" : "差分を表示"}
      </button>
      {expanded && <DiffView entries={entries} leftLabel="自分の版" rightLabel="AI の版" />}
      <ErrorNotice error={error} title="競合を解決できません" />
      <div className="row">
        <button type="button" className="btn primary" onClick={() => resolve("keep_mine")} disabled={busy}>
          自分の版を維持
        </button>
        <button type="button" className="btn" onClick={() => resolve("accept_ai")} disabled={busy}>
          AI の版を採用
        </button>
      </div>
    </section>
  );
}
