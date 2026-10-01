import { describe, expect, it } from "vitest";
import { diffNotes, flattenBullets, groupDiff } from "./noteDiff";
import {
  addAction,
  addTextItem,
  ensureEvidenceInvariant,
  MANUAL_NO_EVIDENCE_REASON,
  prepareForSave,
  removeBullet,
  unconfirmedActions,
  updateAction,
  updateBulletText,
  updateTextItem,
} from "./noteEdit";
import { note } from "./testFixtures";
import type { EvidencedText } from "./types";

describe("diffNotes", () => {
  it("returns nothing for identical notes", () => {
    expect(diffNotes(note(), note())).toEqual([]);
  });

  it("reports changed summary, changed/added actions by id and text-list changes", () => {
    const mine = note();
    const ai = note({
      cornell: { ...mine.cornell, summary: "新しい要約", cues: [{ text: "別の論点", evidenceSegmentIds: ["s1"] }] },
      decisions: [{ ...mine.decisions[0]!, status: "agreed" }],
      actions: [
        { ...mine.actions[0]!, assignee: "佐藤" },
        { id: "a2", text: "追加作業", assignee: null, dueDate: null, status: "open", confirmed: false, evidenceSegmentIds: ["s1"] },
      ],
    });
    const d = diffNotes(mine, ai);
    expect(d).toContainEqual({ section: "summary", kind: "changed", before: "要約", after: "新しい要約" });
    expect(d).toContainEqual({ section: "cues", kind: "removed", before: "論点" });
    expect(d).toContainEqual({ section: "cues", kind: "added", after: "別の論点" });
    expect(d.find((e) => e.section === "decisions")).toMatchObject({ kind: "changed", before: "決定（要確認）", after: "決定（合意）" });
    const actions = d.filter((e) => e.section === "actions");
    expect(actions).toHaveLength(2);
    expect(actions[0]).toMatchObject({ kind: "changed" });
    expect(actions[0]!.after).toContain("担当: 佐藤");
    expect(actions[1]).toMatchObject({ kind: "added" });
    expect(groupDiff(d).map((g) => g.section)).toEqual(["summary", "cues", "decisions", "actions"]);
  });

  it("diffs nested bullets by id", () => {
    const a = note();
    const b = updateBulletText(a, "b2", "子（修正）");
    expect(diffNotes(a, b)).toEqual([{ section: "bullets", kind: "changed", before: "  子", after: "  子（修正）" }]);
    expect(flattenBullets(removeBullet(a, "b2").bullets).map((x) => x.id)).toEqual(["b1"]);
  });

  it("ignores regenerated ids when the content is unchanged", () => {
    const a = note();
    const b = note({
      actions: a.actions.map((x) => ({ ...x, id: "a-new" })),
      bullets: [{ ...a.bullets[0]!, id: "b1-new" }],
    });
    expect(diffNotes(a, b)).toEqual([]);
    const c = note({ actions: [{ ...a.actions[0]!, id: "a-new", assignee: "田中" }] });
    expect(diffNotes(a, c).map((e) => e.kind)).toEqual(["removed", "added"]);
  });

  it("treats duplicate texts as a multiset", () => {
    const a = note({ openQuestions: [{ text: "Q", evidenceSegmentIds: ["s"] }, { text: "Q", evidenceSegmentIds: ["s"] }] });
    const b = note({ openQuestions: [{ text: "Q", evidenceSegmentIds: ["s"] }] });
    expect(diffNotes(a, b)).toEqual([{ section: "openQuestions", kind: "removed", before: "Q" }]);
  });
});

describe("noteEdit", () => {
  it("confirms an AI action without touching other fields", () => {
    const n = updateAction(note(), "a1", { confirmed: true });
    expect(n.actions[0]).toMatchObject({ confirmed: true, origin: "ai", dueDate: "来週金曜", evidenceSegmentIds: ["s2"] });
    expect(unconfirmedActions(n)).toBe(0);
    expect(unconfirmedActions(note())).toBe(1);
  });

  it("adds user items that satisfy the evidence invariant", () => {
    const n = addAction(note(), "手動タスク", "a9");
    expect(n.actions[1]).toMatchObject({ id: "a9", origin: "user", confirmed: true, evidenceSegmentIds: [], noEvidenceReason: MANUAL_NO_EVIDENCE_REASON });
    const q = addTextItem(note(), "cues", "手がかり");
    expect(q.cornell.cues[1]).toMatchObject({ evidenceSegmentIds: [], noEvidenceReason: MANUAL_NO_EVIDENCE_REASON });
    expect(ensureEvidenceInvariant<EvidencedText>({ text: "x", evidenceSegmentIds: [] }).noEvidenceReason).toBe(MANUAL_NO_EVIDENCE_REASON);
  });

  it("edits text items immutably and stamps a new revision on save", () => {
    const base = note();
    const n = updateTextItem(base, "openQuestions", 0, "変更");
    expect(n.openQuestions[0]!.text).toBe("変更");
    expect(base.openQuestions[0]!.text).toBe("未定の点");
    const saved = prepareForSave(n, "2026-09-29T11:00:00+09:00", "33333333-3333-4333-8333-333333333333");
    expect(saved).toMatchObject({ schemaVersion: "1.0", revisionId: "33333333-3333-4333-8333-333333333333", generatedAt: "2026-09-29T11:00:00+09:00" });
  });
});
