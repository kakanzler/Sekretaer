"""Note revisions: AI application with conflict detection (AC-06), user edits, conflict resolution."""

from __future__ import annotations

from typing import Any

from .. import NOTE_SCHEMA_VERSION
from ..errors import ApiError
from ..storage.repo import Repo
from ..summarization.schema import collect_evidence_ids, note_errors
from ..summarization.service import SummaryOutput
from ..util import new_id, now_iso
from .events import EventBus


def revision_to_api(rev: dict[str, Any] | None) -> dict[str, Any] | None:
    if rev is None:
        return None
    return {
        "id": rev["id"],
        "meetingId": rev["meeting_id"],
        "jobId": rev["job_id"],
        "origin": rev["origin"],
        "status": rev["status"],
        "schemaVersion": rev["schema_version"],
        "createdAt": rev["created_at"],
        "supersedesId": rev["supersedes_id"],
        "note": rev.get("payload"),
    }


def revision_meta_to_api(rev: dict[str, Any]) -> dict[str, Any]:
    out = revision_to_api(rev)
    assert out is not None
    out.pop("note", None)
    return out


class NoteService:
    def __init__(self, repo: Repo, bus: EventBus) -> None:
        self.repo = repo
        self.bus = bus

    def get(self, meeting_id: str) -> dict[str, Any]:
        return {
            "current": revision_to_api(self.repo.current_note(meeting_id)),
            "pendingConflict": revision_to_api(self.repo.pending_conflict(meeting_id)),
        }

    def list_revisions(self, meeting_id: str) -> list[dict[str, Any]]:
        return [revision_meta_to_api(r) for r in self.repo.list_note_revisions(meeting_id)]

    # ------------------------------------------------------------------ AI
    def apply_ai(self, job: dict[str, Any], output: SummaryOutput) -> dict[str, Any]:
        mid = job["meeting_id"]
        with self.repo.db.tx():
            current = self.repo.current_note(mid)
            # AC-06: a user edit newer than the note this job worked from must never be overwritten.
            conflict = self.repo.user_revisions_after(mid, output.base_revision_id) > 0
            status = "pending_conflict" if conflict else "applied"
            # Older AI proposals are superseded: either this one applies cleanly on top of the user's
            # revision, or it becomes the single pending proposal.
            for row in self.repo.db.query(
                "SELECT id FROM note_revisions WHERE meeting_id=? AND status='pending_conflict'", (mid,)
            ):
                self.repo.set_note_status(row["id"], "rejected")
            self.repo.insert_note_revision(
                revision_id=output.revision_id, meeting_id=mid, job_id=job["id"], origin="ai", status=status,
                payload=output.note, schema_version=NOTE_SCHEMA_VERSION, source_hash=output.source_hash,
                supersedes_id=current["id"] if current else None,
            )
        rev = self.repo.get_note_revision(mid, output.revision_id)
        assert rev is not None
        return rev

    # ------------------------------------------------------------------ user
    def put_user(self, meeting_id: str, base_revision_id: str | None, note: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(note, dict):
            raise ApiError("invalid_request", details={"errors": ["note must be an object"]})
        with self.repo.db.tx():
            current = self.repo.current_note(meeting_id)
            current_id = current["id"] if current else None
            if base_revision_id != current_id:
                raise ApiError("conflict", details={"currentRevisionId": current_id})
            rev_id = new_id()
            payload = dict(note)
            payload.update({"schemaVersion": NOTE_SCHEMA_VERSION, "meetingId": meeting_id, "revisionId": rev_id,
                            "generatedAt": now_iso()})
            errors = note_errors(payload)
            if errors:
                raise ApiError("invalid_request", "ノートの形式が正しくありません。", details={"errors": errors})
            ids = collect_evidence_ids(payload)
            if ids:
                existing = self.repo.segments_by_ids(meeting_id, sorted(ids))
                missing = sorted(ids - set(existing))
                if missing:
                    raise ApiError("invalid_request", "存在しない発話 ID が根拠に含まれています。",
                                   details={"missingSegmentIds": missing[:20]})
            self.repo.insert_note_revision(
                revision_id=rev_id, meeting_id=meeting_id, job_id=None, origin="user", status="applied",
                payload=payload, schema_version=NOTE_SCHEMA_VERSION, source_hash=None, supersedes_id=current_id,
            )
        rev = self.repo.get_note_revision(meeting_id, rev_id)
        out = revision_to_api(rev)
        assert out is not None
        return out

    def resolve_conflict(self, meeting_id: str, action: str, conflict_revision_id: str) -> dict[str, Any]:
        with self.repo.db.tx():
            pending = self.repo.get_note_revision(meeting_id, conflict_revision_id)
            if pending is None:
                raise ApiError("not_found", details={"resource": "note_revision"})
            if pending["status"] != "pending_conflict":
                raise ApiError("invalid_state", "この競合はすでに解決されています。")
            current = self.repo.current_note(meeting_id)
            if action == "accept_ai":
                self.repo.set_note_status(pending["id"], "applied", supersedes_id=current["id"] if current else None)
                self.repo.touch_note_seq(pending["id"])
                result_id = pending["id"]
            else:
                self.repo.set_note_status(pending["id"], "rejected")
                result_id = current["id"] if current else None
        if result_id is None:
            raise ApiError("invalid_state")
        self.bus.publish("summary.updated", meeting_id, {
            "jobId": pending["job_id"] or "", "revisionId": result_id, "conflict": False, "resolution": action,
        })
        out = revision_to_api(self.repo.get_note_revision(meeting_id, result_id))
        assert out is not None
        return out
