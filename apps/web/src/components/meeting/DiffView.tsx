"use client";

import { groupDiff, NOTE_SECTION_LABEL, type DiffEntry } from "@/lib/noteDiff";
import { plainText } from "@/lib/format";

/**
 * Diff-style comparison of two notes. Every change is labelled in text
 * (「自分の版のみ」 etc.), not only by colour.
 */
export function DiffView({ entries, leftLabel, rightLabel }: { entries: DiffEntry[]; leftLabel: string; rightLabel: string }) {
  if (entries.length === 0) return <p className="muted">内容の違いはありません。</p>;
  return (
    <div className="stack">
      {groupDiff(entries).map((g) => (
        <section key={g.section}>
          <h3>{NOTE_SECTION_LABEL[g.section]}</h3>
          <ul className="diff">
            {g.entries.map((e, i) => {
              if (e.kind === "removed") {
                return (
                  <li key={i} className="del">
                    <span className="tag">− {leftLabel}のみ:</span>
                    {plainText(e.before)}
                  </li>
                );
              }
              if (e.kind === "added") {
                return (
                  <li key={i} className="add">
                    <span className="tag">＋ {rightLabel}のみ:</span>
                    {plainText(e.after)}
                  </li>
                );
              }
              return (
                <li key={i}>
                  <div className="diff-pair">
                    <div className="del">
                      <span className="tag">{leftLabel}:</span>
                      {plainText(e.before)}
                    </div>
                    <div className="add">
                      <span className="tag">{rightLabel}:</span>
                      {plainText(e.after)}
                    </div>
                  </div>
                </li>
              );
            })}
          </ul>
        </section>
      ))}
    </div>
  );
}
