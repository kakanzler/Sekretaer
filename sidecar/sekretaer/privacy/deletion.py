"""Meeting deletion (spec §10 削除と保持, AC-07).

Removes app-managed audio files, temp files and exports, destroys the per-meeting audio key, deletes all
DB rows of the meeting (the meetings row is kept only as a content-free tombstone with state
``deleted``), then checkpoints the WAL so deleted pages leave the log. ``secure_delete`` is on for the
connection, so freed pages are zeroed. Failures are reported per item and yield ``delete_incomplete``.
OS backups and user copies are outside the app's reach.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..logsetup import log
from ..storage.repo import Repo
from ..util import now_iso
from .crypto import AudioCipher

logger = logging.getLogger("sekretaer.deletion")

# Children first; every table with a meeting_id column.
MEETING_TABLES = (
    "events",
    "idempotency_keys",
    "meeting_issues",
    "note_revisions",
    "summary_jobs",
    "transcript_segment_revisions",
    "transcript_segments",
    "audio_chunks",
    "consent_events",
    "audio_sources",
)


def _remove_tree(root: Path, kind: str, failed: list[dict[str, Any]]) -> int:
    if not root.exists():
        return 0
    removed = 0
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        try:
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()
                removed += 1
        except OSError as exc:
            failed.append({"kind": kind, "path": str(path), "reason": type(exc).__name__})
    try:
        root.rmdir()
    except OSError as exc:
        failed.append({"kind": kind, "path": str(root), "reason": type(exc).__name__})
    return removed


def delete_meeting_data(
    *, config: AppConfig, repo: Repo, cipher: AudioCipher | None, meeting_id: str,
) -> dict[str, Any]:
    failed: list[dict[str, Any]] = []
    counts = {"dbRecords": 0, "audioFiles": 0, "tempFiles": 0, "exports": 0}

    # Audio files recorded outside the meeting directory (should not happen, but never leave them behind).
    audio_dir = config.meeting_audio_dir(meeting_id)
    for chunk in repo.list_chunks(meeting_id):
        p = chunk.get("storage_path")
        if p and not Path(p).resolve().is_relative_to(audio_dir.resolve()):
            try:
                Path(p).unlink(missing_ok=True)
                counts["audioFiles"] += 1
            except OSError as exc:
                failed.append({"kind": "audio", "path": p, "reason": type(exc).__name__})

    counts["audioFiles"] += _remove_tree(audio_dir, "audio", failed)
    counts["tempFiles"] += _remove_tree(config.meeting_tmp_dir(meeting_id), "temp", failed)
    counts["exports"] += _remove_tree(config.meeting_exports_dir(meeting_id), "export", failed)

    if cipher is not None:
        try:
            cipher.destroy_key(meeting_id)
        except Exception as exc:  # noqa: BLE001 - keyring backends raise assorted errors
            failed.append({"kind": "key", "path": f"credential:meeting-audio:{meeting_id}",
                           "reason": type(exc).__name__})

    try:
        with repo.db.tx():
            for table in MEETING_TABLES:
                cur = repo.db.execute(f"DELETE FROM {table} WHERE meeting_id=?", (meeting_id,))
                counts["dbRecords"] += max(0, cur.rowcount)
            cur = repo.db.execute(
                "UPDATE meetings SET title='', settings_json='{}', degraded_json='[]', last_error=NULL, "
                "state='deleted', updated_at=? WHERE id=?",
                (now_iso(), meeting_id),
            )
            counts["dbRecords"] += max(0, cur.rowcount)
        repo.db.checkpoint()
    except sqlite3.Error as exc:
        failed.append({"kind": "database", "path": str(config.db_path), "reason": type(exc).__name__})

    status = "deleted" if not failed else "delete_incomplete"
    if failed:
        try:
            repo.update_meeting(meeting_id, state="deleting", last_error="delete_incomplete")
        except sqlite3.Error:
            pass
    log(logger, logging.INFO, "meeting deleted", meetingId=meeting_id, status=status, **counts,
        failures=len(failed))
    return {"deleted": counts, "failed": failed, "status": status}

