// Immutable note editing helpers (pure). Every edit results in a full Note that
// is PUT with the base revision id (api-v1.md PUT /meetings/{id}/notes).

import type { ActionItem, Bullet, Decision, EvidencedText, Note } from "./types";

export const MANUAL_NO_EVIDENCE_REASON = "利用者が手動で追加した項目";

export type TextListKey = "cues" | "notes" | "openQuestions";

export function newUuid(): string {
  return crypto.randomUUID();
}

export function emptyNote(meetingId: string, nowIso: string, revisionId: string = newUuid()): Note {
  return {
    schemaVersion: "1.0",
    meetingId,
    revisionId,
    generatedAt: nowIso,
    coverage: { fromMs: 0, toMs: 0 },
    cornell: { cues: [], notes: [], summary: "" },
    bullets: [],
    decisions: [],
    actions: [],
    openQuestions: [],
  };
}

/** Evidence invariant: empty evidence must carry noEvidenceReason (note schema). */
export function ensureEvidenceInvariant<T extends EvidencedText>(item: T): T {
  if (item.evidenceSegmentIds.length === 0 && !item.noEvidenceReason) {
    return { ...item, noEvidenceReason: MANUAL_NO_EVIDENCE_REASON };
  }
  return item;
}

export function updateAction(note: Note, id: string, patch: Partial<Omit<ActionItem, "id">>): Note {
  return {
    ...note,
    actions: note.actions.map((a) => (a.id === id ? ensureEvidenceInvariant({ ...a, ...patch }) : a)),
  };
}

export function addAction(note: Note, text: string, id: string = newUuid()): Note {
  const item: ActionItem = {
    id,
    text,
    assignee: null,
    dueDate: null,
    status: "open",
    // A user-created item is confirmed by its author; AI-created ones start unconfirmed.
    confirmed: true,
    origin: "user",
    evidenceSegmentIds: [],
    noEvidenceReason: MANUAL_NO_EVIDENCE_REASON,
  };
  return { ...note, actions: [...note.actions, item] };
}

export function removeAction(note: Note, id: string): Note {
  return { ...note, actions: note.actions.filter((a) => a.id !== id) };
}

export function updateDecision(note: Note, id: string, patch: Partial<Omit<Decision, "id">>): Note {
  return {
    ...note,
    decisions: note.decisions.map((d) => (d.id === id ? ensureEvidenceInvariant({ ...d, ...patch }) : d)),
  };
}

export function removeDecision(note: Note, id: string): Note {
  return { ...note, decisions: note.decisions.filter((d) => d.id !== id) };
}

function getTextList(note: Note, key: TextListKey): EvidencedText[] {
  return key === "openQuestions" ? note.openQuestions : note.cornell[key];
}

function setTextList(note: Note, key: TextListKey, list: EvidencedText[]): Note {
  if (key === "openQuestions") return { ...note, openQuestions: list };
  return { ...note, cornell: { ...note.cornell, [key]: list } };
}

export function updateTextItem(note: Note, key: TextListKey, index: number, text: string): Note {
  const list = getTextList(note, key).map((it, i) => (i === index ? { ...it, text } : it));
  return setTextList(note, key, list);
}

export function removeTextItem(note: Note, key: TextListKey, index: number): Note {
  return setTextList(
    note,
    key,
    getTextList(note, key).filter((_, i) => i !== index),
  );
}

export function addTextItem(note: Note, key: TextListKey, text: string): Note {
  const item: EvidencedText = { text, evidenceSegmentIds: [], noEvidenceReason: MANUAL_NO_EVIDENCE_REASON };
  return setTextList(note, key, [...getTextList(note, key), item]);
}

export function setCornellSummary(note: Note, summary: string): Note {
  return { ...note, cornell: { ...note.cornell, summary } };
}

function mapBullets(list: Bullet[], id: string, fn: (b: Bullet) => Bullet | null): Bullet[] {
  const out: Bullet[] = [];
  for (const b of list) {
    if (b.id === id) {
      const r = fn(b);
      if (r) out.push(r);
    } else {
      out.push({ ...b, children: mapBullets(b.children ?? [], id, fn) });
    }
  }
  return out;
}

export function updateBulletText(note: Note, id: string, text: string): Note {
  return { ...note, bullets: mapBullets(note.bullets, id, (b) => ({ ...b, text })) };
}

export function removeBullet(note: Note, id: string): Note {
  return { ...note, bullets: mapBullets(note.bullets, id, () => null) };
}

export function addBullet(note: Note, text: string, id: string = newUuid()): Note {
  const item: Bullet = { id, text, children: [], evidenceSegmentIds: [], noEvidenceReason: MANUAL_NO_EVIDENCE_REASON };
  return { ...note, bullets: [...note.bullets, item] };
}

/**
 * Prepare a user-edited note for PUT: new revisionId and generatedAt. The
 * server records the result as a revision with origin "user".
 */
export function prepareForSave(note: Note, nowIso: string, revisionId: string = newUuid()): Note {
  return { ...note, schemaVersion: "1.0", revisionId, generatedAt: nowIso };
}

/** Unconfirmed AI action count, for the tab badge. */
export function unconfirmedActions(note: Note | null): number {
  return note ? note.actions.filter((a) => !a.confirmed).length : 0;
}
