"use client";

import Link from "next/link";
import { useState } from "react";
import { useConnection, useSidecarEvents } from "@/components/ConnectionProvider";
import { HealthBanner } from "@/components/HealthBanner";
import { Badge, ErrorNotice, Loading } from "@/components/ui";
import { useResource } from "@/hooks/useResource";
import { toApiError, type ApiError } from "@/lib/errors";
import { formatDateTime, formatMs } from "@/lib/format";
import { MEETING_STATE_LABEL, warningLabel } from "@/lib/labels";
import type { Meeting, MeetingState } from "@/lib/types";

const STATE_TONE: Record<MeetingState, "neutral" | "ok" | "warn" | "danger" | "info" | "rec"> = {
  preparing: "neutral",
  recording: "rec",
  finalizing: "info",
  completed: "ok",
  error: "danger",
  deleting: "warn",
  deleted: "neutral",
};

export default function HomePage() {
  const { client } = useConnection();
  const first = useResource("meetings", (c) => c.listMeetings({ limit: 50 }));
  const [more, setMore] = useState<{ items: Meeting[]; nextCursor: string | null } | null>(null);
  const [moreError, setMoreError] = useState<ApiError | null>(null);

  useSidecarEvents((ev) => {
    if (ev.event === "meeting.state") {
      first.reload();
      setMore(null);
    }
  });

  const items = [...(first.data?.items ?? []), ...(more?.items ?? [])];
  const nextCursor = more ? more.nextCursor : (first.data?.nextCursor ?? null);

  async function loadMore() {
    if (!client || !nextCursor) return;
    try {
      const page = await client.listMeetings({ limit: 50, cursor: nextCursor });
      setMore((m) => ({ items: [...(m?.items ?? []), ...page.items], nextCursor: page.nextCursor }));
      setMoreError(null);
    } catch (e) {
      setMoreError(toApiError(e));
    }
  }

  return (
    <div className="stack">
      <HealthBanner />
      <section className="panel" aria-labelledby="list-title">
        <div className="row spread">
          <h1 id="list-title">会議一覧</h1>
          <Link href="/meetings/new/" className="btn primary">
            ＋ 新規会議
          </Link>
        </div>
        <ErrorNotice error={first.error} title="会議一覧を取得できません" />
        {first.loading && <Loading />}
        {first.data && items.length === 0 && <p className="muted">まだ会議はありません。「新規会議」から録音を始めます。</p>}
        {items.length > 0 && (
          <table className="meeting-table">
            <caption className="visually-hidden">会議の一覧（新しい順）</caption>
            <thead>
              <tr>
                <th scope="col">タイトル</th>
                <th scope="col">開始日時</th>
                <th scope="col">長さ</th>
                <th scope="col">処理状態</th>
              </tr>
            </thead>
            <tbody>
              {items.map((m) => (
                <tr key={m.id}>
                  <td>
                    <Link href={`/meetings/view/?id=${encodeURIComponent(m.id)}`}>{m.title || "（無題）"}</Link>
                  </td>
                  <td>{m.startedAt ? formatDateTime(m.startedAt, m.timezone) : <span className="muted">未開始（作成 {formatDateTime(m.createdAt, m.timezone)}）</span>}</td>
                  <td className="seg-time">{m.startedAt ? formatMs(m.elapsedMs) : "—"}</td>
                  <td>
                    <div className="row">
                      <Badge tone={STATE_TONE[m.state]}>{MEETING_STATE_LABEL[m.state] ?? m.state}</Badge>
                      {m.processing.pendingJobs > 0 && <Badge tone="info">要約待ち {m.processing.pendingJobs} 件</Badge>}
                      {m.degraded.map((d) => (
                        <Badge key={d} tone="warn">
                          {warningLabel(d)}
                        </Badge>
                      ))}
                    </div>
                    {m.processing.lastError && <p className="small error-text">最後のエラー: {m.processing.lastError}</p>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <ErrorNotice error={moreError} />
        {nextCursor && (
          <button type="button" className="btn" onClick={loadMore}>
            さらに読み込む
          </button>
        )}
      </section>
    </div>
  );
}
