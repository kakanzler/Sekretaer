"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { MeetingWorkspace } from "@/components/meeting/MeetingWorkspace";
import { Loading } from "@/components/ui";

// Live meeting screen and post-meeting detail share this route. Static export
// cannot pre-render unknown dynamic segments, so the id is a query parameter.
function MeetingView() {
  const id = useSearchParams().get("id");
  if (!id) {
    return (
      <section className="panel">
        <h1>会議が指定されていません</h1>
        <Link href="/">会議一覧へ</Link>
      </section>
    );
  }
  return <MeetingWorkspace key={id} meetingId={id} />;
}

export default function MeetingViewPage() {
  return (
    <Suspense fallback={<Loading />}>
      <MeetingView />
    </Suspense>
  );
}
