"""Incremental structured summarization for one job (spec §6, AC-04/AC-05).

Input = finalized segments after the current note's coverage up to the job's range end (+ a little
overlap context), the previous note, meeting metadata and the previous revision id. Long input is
chunked by ``maxInputChars``; each chunk is one CLI call validated on its own with at most one repair
re-prompt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator

from ..logsetup import log
from ..storage.repo import Repo
from ..util import new_id, now_iso, sha256_hex
from .cli import ClaudeCli, CliError
from .draft import (
    MapContext,
    build_alias_table,
    draft_to_note,
    extract_json_object,
    note_to_draft,
    segment_view,
)
from .prompt import DRAFT_SCHEMA, REPAIR_PROMPT, USER_PROMPT, build_payload
from .schema import collect_evidence_ids, note_errors

logger = logging.getLogger("sekretaer.summarizer")
CONTEXT_SEGMENTS = 2
_draft_validator = Draft202012Validator(DRAFT_SCHEMA)


@dataclass
class SummaryInput:
    meeting: dict[str, Any]
    previous: dict[str, Any] | None  # note revision row (payload + meta)
    segments: list[dict[str, Any]]
    context: list[dict[str, Any]]
    source_kinds: dict[str, str]
    range_end_ms: int


@dataclass
class SummaryOutput:
    note: dict[str, Any]
    revision_id: str
    base_revision_id: str | None
    source_hash: str
    dropped_evidence: int
    calls: int


class Summarizer:
    def __init__(self, repo: Repo, cli: ClaudeCli, max_input_chars: int = 24_000) -> None:
        self.repo = repo
        self.cli = cli
        self.max_input_chars = max_input_chars

    # ------------------------------------------------------------------ input
    def collect(self, meeting_id: str, range_end_ms: int | None) -> SummaryInput:
        meeting = self.repo.get_meeting(meeting_id)
        if meeting is None:
            raise CliError("not_found")
        previous = self.repo.current_note(meeting_id)
        covered = int(previous["payload"]["coverage"]["toMs"]) if previous else 0
        segments = self.repo.final_segments_in_range(meeting_id, covered, range_end_ms)
        context = self.repo.final_segments_before(meeting_id, covered, CONTEXT_SEGMENTS) if covered > 0 else []
        kinds = {s["id"]: s["kind"] for s in self.repo.list_sources(meeting_id)}
        return SummaryInput(meeting=meeting, previous=previous, segments=segments, context=context,
                            source_kinds=kinds, range_end_ms=range_end_ms or 0)

    def chunks(self, segments: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        out: list[list[dict[str, Any]]] = []
        cur: list[dict[str, Any]] = []
        size = 0
        for seg in segments:
            n = len(seg["text"]) + 64
            if cur and size + n > self.max_input_chars:
                out.append(cur)
                cur, size = [], 0
            cur.append(seg)
            size += n
        if cur:
            out.append(cur)
        return out

    def render_payload(
        self, inp: SummaryInput, chunk: list[dict[str, Any]], context: list[dict[str, Any]],
        working: dict[str, Any] | None, index: int, count: int, repair: dict[str, Any] | None = None,
    ) -> tuple[str, Any]:
        prev_ids = sorted(collect_evidence_ids(working)) if working else []
        prev_segments = list(self.repo.segments_by_ids(inp.meeting["id"], prev_ids).values()) if prev_ids else []
        prev_segments.sort(key=lambda s: (s["start_ms"], s["id"]))
        table = build_alias_table(context=context, transcript=chunk, previous_segments=prev_segments)
        kind = inp.source_kinds

        def view(seg: dict[str, Any]) -> dict[str, Any]:
            return segment_view(table.id_to_alias[seg["id"]], seg, kind.get(seg["source_id"], "microphone"))

        payload = build_payload(
            meeting={"title": inp.meeting["title"], "language": inp.meeting["language"]},
            previous_note=note_to_draft(working, table),
            transcript=[view(s) for s in chunk],
            context=[view(s) for s in context],
            chunk_index=index,
            chunk_count=count,
            repair=repair,
        )
        return payload, table

    def preview(self, meeting_id: str) -> dict[str, Any]:
        inp = self.collect(meeting_id, None)
        if not inp.segments:
            return {"fromMs": 0, "toMs": 0, "segmentCount": 0, "text": ""}
        chunks = self.chunks(inp.segments)
        working = inp.previous["payload"] if inp.previous else None
        text, _ = self.render_payload(inp, chunks[0], inp.context, working, 1, len(chunks))
        return {
            "fromMs": int(inp.segments[0]["start_ms"]),
            "toMs": int(inp.segments[-1]["end_ms"]),
            "segmentCount": len(inp.segments),
            "chunkCount": len(chunks),
            "text": text,
        }

    # ------------------------------------------------------------------ run
    async def run(self, job: dict[str, Any]) -> SummaryOutput | None:
        inp = self.collect(job["meeting_id"], int(job["range_end_ms"]))
        if not inp.segments:
            return None
        previous = inp.previous
        base_id = previous["id"] if previous else None
        working: dict[str, Any] | None = previous["payload"] if previous else None
        revision_id = new_id()
        coverage_from = int(working["coverage"]["fromMs"]) if working and working["coverage"]["toMs"] > 0 else int(
            inp.segments[0]["start_ms"])
        coverage_from = min(coverage_from, int(inp.segments[0]["start_ms"]))
        chunks = self.chunks(inp.segments)
        context = inp.context
        hashes = []
        dropped = 0
        calls = 0
        for i, chunk in enumerate(chunks, start=1):
            payload, table = self.render_payload(inp, chunk, context, working, i, len(chunks))
            hashes.append(sha256_hex(payload))
            ctx = MapContext(
                meeting_id=inp.meeting["id"], revision_id=revision_id, generated_at=now_iso(),
                coverage_from_ms=coverage_from,
                coverage_to_ms=max(int(working["coverage"]["toMs"]) if working else 0, int(chunk[-1]["end_ms"])),
                section_from_ms=int(chunk[0]["start_ms"]), section_to_ms=int(chunk[-1]["end_ms"]),
                previous_note=working, table=table,
            )
            note, n_calls = await self._call_with_repair(job["id"], payload, ctx, inp, chunk, context, working, i,
                                                         len(chunks))
            calls += n_calls
            dropped += ctx.dropped_ids
            working = note
            context = chunk[-CONTEXT_SEGMENTS:]
        assert working is not None
        log(logger, logging.INFO, "summary produced", jobId=job["id"], chunks=len(chunks), calls=calls,
            droppedEvidence=dropped, segments=len(inp.segments))
        return SummaryOutput(note=working, revision_id=revision_id, base_revision_id=base_id,
                             source_hash=sha256_hex("".join(hashes)), dropped_evidence=dropped, calls=calls)

    async def _call_with_repair(
        self, key: str, payload: str, ctx: MapContext, inp: SummaryInput, chunk: list[dict[str, Any]],
        context: list[dict[str, Any]], working: dict[str, Any] | None, index: int, count: int,
    ) -> tuple[dict[str, Any], int]:
        result = await self.cli.run(payload, USER_PROMPT, key=key)
        note, errors = self._to_note(result.text, ctx)
        if note is not None:
            return note, 1
        log(logger, logging.WARNING, "summary output invalid, repairing", jobId=key, errorCount=len(errors))
        repair = {"errors": errors[:10], "previousReply": result.text[:8000]}
        payload2, table2 = self.render_payload(inp, chunk, context, working, index, count, repair=repair)
        ctx.table = table2
        ctx.dropped_ids = 0
        result2 = await self.cli.run(payload2, REPAIR_PROMPT, key=key)
        note2, errors2 = self._to_note(result2.text, ctx)
        if note2 is None:
            raise CliError("summary_invalid_schema", f"{len(errors2)} validation errors after repair")
        return note2, 2

    def _to_note(self, text: str, ctx: MapContext) -> tuple[dict[str, Any] | None, list[str]]:
        try:
            draft = extract_json_object(text)
        except ValueError:
            return None, ["reply is not a JSON object"]
        if not isinstance(draft, dict):
            return None, ["reply is not a JSON object"]
        draft_errors = [
            f"{'/'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message[:200]}"
            for e in list(_draft_validator.iter_errors(draft))[:10]
        ]
        if draft_errors:
            return None, draft_errors
        note = draft_to_note(draft, ctx)
        errors = note_errors(note)
        if errors:
            return None, errors
        # AC-05: every evidence id must be a real segment of this meeting.
        ids = collect_evidence_ids(note)
        if ids:
            existing = self.repo.segments_by_ids(ctx.meeting_id, sorted(ids))
            missing = ids - set(existing)
            if missing:
                return None, [f"{len(missing)} evidence ids do not exist"]
        return note, []
