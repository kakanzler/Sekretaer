"""Alias handling and draft -> note mapping (AC-05).

The model never sees segment UUIDs. It gets short aliases (S1, S2, ... for utterances in the input,
P1, ... for older utterances cited by the previous note) and item handles (B1, D1, A1). The server maps
aliases back, drops ids that do not exist, assigns item/revision/meeting ids and generatedAt, and the
caller validates the result strictly against note-1.0.schema.json.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..util import new_id

NO_EVIDENCE_DEFAULT = "根拠となる発話を特定できませんでした。"
NO_EVIDENCE_DROPPED = "示された根拠 ID が存在しないため除外しました。"
CERTAINTY_LABELS = ("stated", "inferred", "uncertain")
DECISION_STATUSES = ("proposed", "agreed", "withdrawn", "needs_review")
ACTION_STATUSES = ("open", "in_progress", "done", "canceled")
AGREED_MIN_CERTAINTY = 0.7
MAX_BULLET_DEPTH = 4
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


@dataclass
class AliasTable:
    alias_to_id: dict[str, str] = field(default_factory=dict)
    id_to_alias: dict[str, str] = field(default_factory=dict)
    start_ms: dict[str, int] = field(default_factory=dict)  # segment id -> start
    refs: dict[str, str] = field(default_factory=dict)  # item handle -> item uuid

    def add_segment(self, alias: str, seg: dict[str, Any]) -> None:
        self.alias_to_id[alias] = seg["id"]
        self.id_to_alias[seg["id"]] = alias
        self.start_ms[seg["id"]] = int(seg["start_ms"])

    def resolve(self, token: Any) -> str | None:
        if not isinstance(token, str):
            return None
        t = token.strip()
        if t in self.alias_to_id:
            return self.alias_to_id[t]
        if _UUID.match(t) and t in self.id_to_alias:
            return t
        return None


def segment_view(alias: str, seg: dict[str, Any], source_kind: str) -> dict[str, Any]:
    return {
        "id": alias,
        "startMs": int(seg["start_ms"]),
        "endMs": int(seg["end_ms"]),
        "source": source_kind,
        "speaker": seg.get("speaker_label"),
        "text": seg["text"],
    }


def build_alias_table(
    *, context: list[dict[str, Any]], transcript: list[dict[str, Any]], previous_segments: list[dict[str, Any]],
) -> AliasTable:
    table = AliasTable()
    n = 0
    for seg in [*context, *transcript]:
        if seg["id"] in table.id_to_alias:
            continue
        n += 1
        table.add_segment(f"S{n}", seg)
    p = 0
    for seg in previous_segments:
        if seg["id"] in table.id_to_alias:
            continue
        p += 1
        table.add_segment(f"P{p}", seg)
    return table


# ---------------------------------------------------------------- previous note -> draft


def note_to_draft(note: dict[str, Any] | None, table: AliasTable) -> dict[str, Any] | None:
    if not note:
        return None

    def ev(item: dict[str, Any]) -> list[str]:
        return [table.id_to_alias[i] for i in item.get("evidenceSegmentIds", []) if i in table.id_to_alias]

    counters = {"B": 0, "D": 0, "A": 0}

    def handle(prefix: str, item_id: str) -> str:
        counters[prefix] += 1
        h = f"{prefix}{counters[prefix]}"
        table.refs[h] = item_id
        return h

    def bullet(b: dict[str, Any]) -> dict[str, Any]:
        out = {"ref": handle("B", b["id"]), "text": b["text"], "evidence": ev(b),
               "children": [bullet(c) for c in b.get("children", [])]}
        if b.get("certainty"):
            out["certainty"] = b["certainty"]
        return out

    cornell = note.get("cornell", {})
    return {
        "cornell": {
            "cues": [{"text": c["text"], "evidence": ev(c)} for c in cornell.get("cues", [])],
            "notes": [{"text": c["text"], "evidence": ev(c), "certainty": c.get("certainty")}
                      for c in cornell.get("notes", [])],
            "summary": cornell.get("summary", ""),
        },
        "bullets": [bullet(b) for b in note.get("bullets", [])],
        "decisions": [
            {"ref": handle("D", d["id"]), "text": d["text"], "status": d["status"], "certainty": d["certainty"],
             "evidence": ev(d)}
            for d in note.get("decisions", [])
        ],
        "actions": [
            {"ref": handle("A", a["id"]), "text": a["text"], "assignee": a.get("assignee"),
             "dueDate": a.get("dueDate"), "status": a["status"], "evidence": ev(a),
             **({"confirmedByUser": True} if a.get("confirmed") or a.get("origin") == "user" else {})}
            for a in note.get("actions", [])
        ],
        "openQuestions": [{"text": q["text"], "evidence": ev(q)} for q in note.get("openQuestions", [])],
    }


# ---------------------------------------------------------------- draft -> note


@dataclass
class MapContext:
    meeting_id: str
    revision_id: str
    generated_at: str
    coverage_from_ms: int
    coverage_to_ms: int
    section_from_ms: int
    section_to_ms: int
    previous_note: dict[str, Any] | None
    table: AliasTable
    dropped_ids: int = 0


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    t = value.strip()
    return t or None


def _evidence(ctx: MapContext, item: dict[str, Any]) -> tuple[list[str], str | None]:
    raw = item.get("evidence")
    if raw is None:
        raw = item.get("evidenceSegmentIds", [])
    if not isinstance(raw, list):
        raw = []
    ids: list[str] = []
    dropped = 0
    for token in raw:
        resolved = ctx.table.resolve(token)
        if resolved is None:
            dropped += 1
            continue
        if resolved not in ids:
            ids.append(resolved)
    ctx.dropped_ids += dropped
    if ids:
        return ids, None
    reason = _text(item.get("noEvidenceReason"))
    if reason is None:
        reason = NO_EVIDENCE_DROPPED if dropped else NO_EVIDENCE_DEFAULT
    return [], reason


def _evidenced(ctx: MapContext, item: dict[str, Any]) -> dict[str, Any] | None:
    text = _text(item.get("text"))
    if text is None:
        return None
    ids, reason = _evidence(ctx, item)
    out: dict[str, Any] = {"text": text, "evidenceSegmentIds": ids}
    if reason is not None:
        out["noEvidenceReason"] = reason
    return out


def _item_id(ctx: MapContext, item: dict[str, Any], used: set[str]) -> str:
    ref = item.get("ref")
    if isinstance(ref, str) and ref in ctx.table.refs and ctx.table.refs[ref] not in used:
        iid = ctx.table.refs[ref]
    else:
        iid = new_id()
    used.add(iid)
    return iid


def _bullet(ctx: MapContext, b: Any, used: set[str], depth: int) -> dict[str, Any] | None:
    if not isinstance(b, dict):
        return None
    base = _evidenced(ctx, b)
    if base is None:
        return None
    out = {"id": _item_id(ctx, b, used), **base, "children": []}
    cert = b.get("certainty")
    if cert in CERTAINTY_LABELS:
        out["certainty"] = cert
    if depth < MAX_BULLET_DEPTH:
        for c in b.get("children") or []:
            child = _bullet(ctx, c, used, depth + 1)
            if child is not None:
                out["children"].append(child)
    return out


def _clamp01(v: Any, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:  # NaN
        return default
    return min(1.0, max(0.0, f))


def draft_to_note(draft: dict[str, Any], ctx: MapContext) -> dict[str, Any]:
    used: set[str] = set()
    cornell_in = draft.get("cornell") or {}

    cues = [x for c in cornell_in.get("cues") or [] if isinstance(c, dict) and (x := _evidenced(ctx, c))]
    notes = []
    for n in cornell_in.get("notes") or []:
        if not isinstance(n, dict):
            continue
        x = _evidenced(ctx, n)
        if x is None:
            continue
        if n.get("certainty") in CERTAINTY_LABELS:
            x["certainty"] = n["certainty"]
        notes.append(x)

    prev_sections = []
    if ctx.previous_note:
        prev_sections = list((ctx.previous_note.get("cornell") or {}).get("sections") or [])
    section_summary = _text(cornell_in.get("sectionSummary"))
    # A re-run over the same range replaces its section instead of duplicating it.
    sections = [s for s in prev_sections if s.get("fromMs") != ctx.section_from_ms]
    if section_summary:
        sections.append({"fromMs": ctx.section_from_ms, "toMs": ctx.section_to_ms, "summary": section_summary})

    cornell: dict[str, Any] = {
        "cues": cues,
        "notes": notes,
        "summary": cornell_in.get("summary") if isinstance(cornell_in.get("summary"), str) else "",
    }
    if sections:
        cornell["sections"] = sections

    bullets = [x for b in draft.get("bullets") or [] if (x := _bullet(ctx, b, used, 1))]

    decisions = []
    for d in draft.get("decisions") or []:
        if not isinstance(d, dict):
            continue
        base = _evidenced(ctx, d)
        if base is None:
            continue
        certainty = _clamp01(d.get("certainty"), 0.5)
        status = d.get("status") if d.get("status") in DECISION_STATUSES else "needs_review"
        # The AI may only assert agreement when it is confident and can point at the utterance.
        if status == "agreed" and (certainty < AGREED_MIN_CERTAINTY or not base["evidenceSegmentIds"]):
            status = "needs_review"
        item = {"id": _item_id(ctx, d, used), **base, "status": status, "certainty": certainty}
        if base["evidenceSegmentIds"]:
            item["atMs"] = min(ctx.table.start_ms[i] for i in base["evidenceSegmentIds"])
        decisions.append(item)

    prev_actions = {a["id"]: a for a in (ctx.previous_note or {}).get("actions", [])}
    protected = {aid: a for aid, a in prev_actions.items() if a.get("confirmed") or a.get("origin") == "user"}
    actions = []
    for a in draft.get("actions") or []:
        if not isinstance(a, dict):
            continue
        ref = a.get("ref")
        ref_id = ctx.table.refs.get(ref) if isinstance(ref, str) else None
        if ref_id in protected and ref_id not in used:
            # User-confirmed / user-authored actions are never rewritten by the AI.
            actions.append(protected[ref_id])
            used.add(ref_id)
            continue
        base = _evidenced(ctx, a)
        if base is None:
            continue
        assignee = _text(a.get("assignee"))
        due = a.get("dueDate") if isinstance(a.get("dueDate"), str) and a.get("dueDate").strip() else None
        status = a.get("status") if a.get("status") in ACTION_STATUSES else "open"
        actions.append({
            "id": _item_id(ctx, a, used), **base, "assignee": assignee, "dueDate": due, "status": status,
            "confirmed": False, "origin": "ai",
        })
    for aid, a in protected.items():
        if aid not in used:
            actions.append(a)
            used.add(aid)

    questions = [x for q in draft.get("openQuestions") or [] if isinstance(q, dict) and (x := _evidenced(ctx, q))]

    return {
        "schemaVersion": "1.0",
        "meetingId": ctx.meeting_id,
        "revisionId": ctx.revision_id,
        "generatedAt": ctx.generated_at,
        "coverage": {"fromMs": ctx.coverage_from_ms, "toMs": ctx.coverage_to_ms},
        "cornell": cornell,
        "bullets": bullets,
        "decisions": decisions,
        "actions": actions,
        "openQuestions": questions,
    }


def empty_note(meeting_id: str, revision_id: str, generated_at: str) -> dict[str, Any]:
    return {
        "schemaVersion": "1.0",
        "meetingId": meeting_id,
        "revisionId": revision_id,
        "generatedAt": generated_at,
        "coverage": {"fromMs": 0, "toMs": 0},
        "cornell": {"cues": [], "notes": [], "summary": ""},
        "bullets": [],
        "decisions": [],
        "actions": [],
        "openQuestions": [],
    }


def extract_json_object(text: str) -> Any:
    """Parse the model reply. Accepts a bare object or one wrapped in a Markdown fence."""
    import json

    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in reply")
    return json.loads(s[start : end + 1])
