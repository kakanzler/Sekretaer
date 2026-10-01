"use client";

import { createContext, useContext } from "react";
import { formatMs } from "@/lib/format";
import type { TranscriptState } from "@/lib/transcript";
import type { EvidencedText } from "@/lib/types";

export interface EvidenceContextValue {
  transcript: TranscriptState;
  jumpTo: (segmentId: string) => void;
}

export const EvidenceContext = createContext<EvidenceContextValue | null>(null);

/** Evidence chips: each jumps to and highlights the referenced transcript segment. */
export function EvidenceChips({ item }: { item: EvidencedText }) {
  const ctx = useContext(EvidenceContext);
  if (item.evidenceSegmentIds.length === 0) {
    return <span className="no-evidence">根拠なし{item.noEvidenceReason ? `: ${item.noEvidenceReason}` : ""}</span>;
  }
  return (
    <span className="chips" aria-label="根拠発話">
      {item.evidenceSegmentIds.map((id) => {
        const seg = ctx?.transcript.byId[id];
        if (!seg) {
          return (
            <button key={id} type="button" className="chip" disabled title="逐語記録に見つかりません">
              根拠（未検出）
            </button>
          );
        }
        return (
          <button
            key={id}
            type="button"
            className="chip"
            onClick={() => ctx?.jumpTo(id)}
            aria-label={`根拠 ${formatMs(seg.startMs)} の発話へ移動`}
          >
            ▶ {formatMs(seg.startMs)}
          </button>
        );
      })}
    </span>
  );
}
