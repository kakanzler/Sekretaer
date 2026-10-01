// Structural diff between two notes (pure). Used for the AI-vs-manual conflict
// banner (AC-06) and the save-conflict dialog.

import { ACTION_STATUS_LABEL, CERTAINTY_LABEL, DECISION_STATUS_LABEL } from "./labels";
import type { ActionItem, Bullet, Decision, EvidencedText, Note } from "./types";

export type NoteSectionKey = "summary" | "cues" | "notes" | "sections" | "bullets" | "decisions" | "actions" | "openQuestions";

export const NOTE_SECTION_LABEL: Record<NoteSectionKey, string> = {
  summary: "Cornell 要約",
  cues: "Cornell 手がかり",
  notes: "Cornell ノート",
  sections: "区間要約",
  bullets: "箇条書き",
  decisions: "決定事項",
  actions: "アクション",
  openQuestions: "未解決",
};

export interface DiffEntry {
  section: NoteSectionKey;
  kind: "added" | "removed" | "changed";
  /** Text in `a` (base / mine) */
  before?: string;
  /** Text in `b` (other / AI) */
  after?: string;
}

export function describeAction(a: ActionItem): string {
  return `${a.text}（担当: ${a.assignee ?? "未定"} / 期限: ${a.dueDate ?? "未定"} / ${ACTION_STATUS_LABEL[a.status] ?? a.status} / ${a.confirmed ? "確認済み" : "未確認"}）`;
}

export function describeDecision(d: Decision): string {
  return `${d.text}（${DECISION_STATUS_LABEL[d.status] ?? d.status}）`;
}

function describeEvidenced(e: EvidencedText & { certainty?: string }): string {
  const c = e.certainty && e.certainty in CERTAINTY_LABEL ? `（${CERTAINTY_LABEL[e.certainty as keyof typeof CERTAINTY_LABEL]}）` : "";
  return `${e.text}${c}`;
}

/** Multiset difference by rendered text, for lists without stable ids. */
function diffByText(section: NoteSectionKey, a: string[], b: string[]): DiffEntry[] {
  const countB = new Map<string, number>();
  for (const s of b) countB.set(s, (countB.get(s) ?? 0) + 1);
  const out: DiffEntry[] = [];
  const remainingB = new Map(countB);
  for (const s of a) {
    const n = remainingB.get(s) ?? 0;
    if (n > 0) remainingB.set(s, n - 1);
    else out.push({ section, kind: "removed", before: s });
  }
  for (const [s, n] of remainingB) for (let i = 0; i < n; i++) out.push({ section, kind: "added", after: s });
  return out;
}

/** Diff for lists with ids, preserving b's order for additions. */
function diffById<T extends { id: string }>(
  section: NoteSectionKey,
  a: T[],
  b: T[],
  describe: (x: T) => string,
): DiffEntry[] {
  const mapA = new Map(a.map((x) => [x.id, x]));
  const mapB = new Map(b.map((x) => [x.id, x]));
  const out: DiffEntry[] = [];
  for (const x of a) {
    const y = mapB.get(x.id);
    if (!y) out.push({ section, kind: "removed", before: describe(x) });
    else {
      const dx = describe(x);
      const dy = describe(y);
      if (dx !== dy) out.push({ section, kind: "changed", before: dx, after: dy });
    }
  }
  for (const y of b) if (!mapA.has(y.id)) out.push({ section, kind: "added", after: describe(y) });
  return cancelIdenticalMoves(out);
}

/**
 * A new AI revision may regenerate ids for unchanged items. Pair a "removed"
 * and an "added" entry with the same rendered text so they cancel out.
 */
function cancelIdenticalMoves(entries: DiffEntry[]): DiffEntry[] {
  const addedByText = new Map<string, number[]>();
  entries.forEach((e, i) => {
    if (e.kind === "added" && e.after !== undefined) {
      const list = addedByText.get(e.after) ?? [];
      list.push(i);
      addedByText.set(e.after, list);
    }
  });
  const drop = new Set<number>();
  entries.forEach((e, i) => {
    if (e.kind !== "removed" || e.before === undefined) return;
    const list = addedByText.get(e.before);
    const j = list?.shift();
    if (j !== undefined) {
      drop.add(i);
      drop.add(j);
    }
  });
  return entries.filter((_, i) => !drop.has(i));
}

export interface FlatBullet {
  id: string;
  depth: number;
  text: string;
}

export function flattenBullets(bullets: Bullet[], depth = 0, out: FlatBullet[] = []): FlatBullet[] {
  for (const b of bullets) {
    out.push({ id: b.id, depth, text: describeEvidenced(b) });
    flattenBullets(b.children ?? [], depth + 1, out);
  }
  return out;
}

export function diffNotes(a: Note, b: Note): DiffEntry[] {
  const out: DiffEntry[] = [];
  if ((a.cornell.summary ?? "") !== (b.cornell.summary ?? "")) {
    out.push({ section: "summary", kind: "changed", before: a.cornell.summary, after: b.cornell.summary });
  }
  out.push(...diffByText("cues", a.cornell.cues.map(describeEvidenced), b.cornell.cues.map(describeEvidenced)));
  out.push(...diffByText("notes", a.cornell.notes.map(describeEvidenced), b.cornell.notes.map(describeEvidenced)));
  const secA = (a.cornell.sections ?? []).map((s) => s.summary);
  const secB = (b.cornell.sections ?? []).map((s) => s.summary);
  out.push(...diffByText("sections", secA, secB));
  out.push(
    ...diffById(
      "bullets",
      flattenBullets(a.bullets),
      flattenBullets(b.bullets),
      (x) => `${"  ".repeat(x.depth)}${x.text}`,
    ),
  );
  out.push(...diffById("decisions", a.decisions, b.decisions, describeDecision));
  out.push(...diffById("actions", a.actions, b.actions, describeAction));
  out.push(
    ...diffByText("openQuestions", a.openQuestions.map(describeEvidenced), b.openQuestions.map(describeEvidenced)),
  );
  return out;
}

export function groupDiff(entries: DiffEntry[]): { section: NoteSectionKey; entries: DiffEntry[] }[] {
  const order: NoteSectionKey[] = ["summary", "cues", "notes", "sections", "bullets", "decisions", "actions", "openQuestions"];
  return order
    .map((section) => ({ section, entries: entries.filter((e) => e.section === section) }))
    .filter((g) => g.entries.length > 0);
}
