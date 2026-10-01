"""Repositories over the SQLite schema. All methods run on the caller's thread; the Database lock
serialises access. Callers that need atomicity across calls wrap them in ``db.tx()``."""

from __future__ import annotations

import sqlite3
from typing import Any

from ..util import dumps, loads, new_id, now_iso
from .db import Database

ACTIVE_JOB_STATUSES = ("queued", "running", "retry_wait")
CLAIMING_JOB_STATUSES = ("queued", "running", "retry_wait", "succeeded")


def _d(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)


class Repo:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ meetings
    def insert_meeting(
        self, *, title: str, language: str, timezone: str, settings: dict[str, Any], meeting_id: str | None = None
    ) -> str:
        mid = meeting_id or new_id()
        now = now_iso()
        self.db.execute(
            "INSERT INTO meetings(id,title,language,state,degraded_json,timezone,settings_json,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (mid, title, language, "preparing", "[]", timezone, dumps(settings), now, now),
        )
        return mid

    def get_meeting(self, meeting_id: str) -> dict[str, Any] | None:
        row = _d(self.db.one("SELECT * FROM meetings WHERE id=?", (meeting_id,)))
        if row is not None:
            row["settings"] = loads(row.pop("settings_json"), {})
            row["degraded"] = loads(row.pop("degraded_json"), [])
        return row

    def list_meetings(self, limit: int, cursor: tuple[str, str] | None) -> list[dict[str, Any]]:
        sql = "SELECT id FROM meetings WHERE state != 'deleted'"
        params: list[Any] = []
        if cursor is not None:
            sql += " AND (created_at < ? OR (created_at = ? AND id < ?))"
            params += [cursor[0], cursor[0], cursor[1]]
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        return [m for r in self.db.query(sql, params) if (m := self.get_meeting(r["id"])) is not None]

    def update_meeting(self, meeting_id: str, **fields: Any) -> None:
        if not fields:
            return
        cols = []
        values: list[Any] = []
        for key, value in fields.items():
            if key == "settings":
                cols.append("settings_json=?")
                values.append(dumps(value))
            elif key == "degraded":
                cols.append("degraded_json=?")
                values.append(dumps(sorted(set(value))))
            else:
                cols.append(f"{key}=?")
                values.append(value)
        cols.append("updated_at=?")
        values.append(now_iso())
        values.append(meeting_id)
        self.db.execute(f"UPDATE meetings SET {', '.join(cols)} WHERE id=?", values)

    def meetings_in_states(self, states: tuple[str, ...]) -> list[dict[str, Any]]:
        marks = ",".join("?" for _ in states)
        rows = self.db.query(f"SELECT id FROM meetings WHERE state IN ({marks})", states)
        return [m for r in rows if (m := self.get_meeting(r["id"])) is not None]

    # ------------------------------------------------------------------ sources
    def insert_source(self, meeting_id: str, kind: str, device_key: str) -> str:
        sid = new_id()
        self.db.execute(
            "INSERT INTO audio_sources(id,meeting_id,kind,device_key,permission_state,offset_ms,created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (sid, meeting_id, kind, device_key, "unknown", 0, now_iso()),
        )
        return sid

    def list_sources(self, meeting_id: str) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM audio_sources WHERE meeting_id=? ORDER BY created_at, id", (meeting_id,))
        return [dict(r) for r in rows]

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        return _d(self.db.one("SELECT * FROM audio_sources WHERE id=?", (source_id,)))

    def update_source(self, source_id: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE audio_sources SET {cols} WHERE id=?", [*fields.values(), source_id])

    # ------------------------------------------------------------------ consent
    def insert_consent(self, meeting_id: str, scope: str, granted: bool, policy_version: str, event_type: str) -> str:
        cid = new_id()
        self.db.execute(
            "INSERT INTO consent_events(id,meeting_id,event_type,scope,granted,recorded_at,policy_version)"
            " VALUES (?,?,?,?,?,?,?)",
            (cid, meeting_id, event_type, scope, 1 if granted else 0, now_iso(), policy_version),
        )
        return cid

    def consent_state(self, meeting_id: str) -> dict[str, str]:
        state = {"recording": "unconfirmed", "external_processing": "unconfirmed"}
        rows = self.db.query(
            "SELECT scope, granted FROM consent_events WHERE meeting_id=? ORDER BY recorded_at, rowid", (meeting_id,)
        )
        for r in rows:
            state[r["scope"]] = "granted" if r["granted"] else "denied"
        return state

    def list_consent(self, meeting_id: str) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM consent_events WHERE meeting_id=? ORDER BY recorded_at, rowid",
                             (meeting_id,))
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ segments
    def upsert_partial_segment(
        self, *, segment_id: str, meeting_id: str, source_id: str, start_ms: int, end_ms: int, text: str,
        language: str | None,
    ) -> None:
        now = now_iso()
        self.db.execute(
            "INSERT INTO transcript_segments(id,meeting_id,source_id,start_ms,end_ms,text,language,is_final,revision,"
            "stt_metadata_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,0,0,'{}',?,?) "
            "ON CONFLICT(id) DO UPDATE SET end_ms=excluded.end_ms, text=excluded.text, "
            "language=excluded.language, updated_at=excluded.updated_at WHERE is_final=0",
            (segment_id, meeting_id, source_id, start_ms, end_ms, text, language, now, now),
        )

    def finalize_segment(
        self, *, segment_id: str, meeting_id: str, source_id: str, start_ms: int, end_ms: int, text: str,
        language: str | None, stt_metadata: dict[str, Any],
    ) -> None:
        now = now_iso()
        with self.db.tx():
            self.db.execute(
                "INSERT INTO transcript_segments(id,meeting_id,source_id,start_ms,end_ms,text,language,is_final,"
                "revision,stt_metadata_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,1,1,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET start_ms=excluded.start_ms, end_ms=excluded.end_ms, text=excluded.text,"
                " language=excluded.language, is_final=1, revision=1, stt_metadata_json=excluded.stt_metadata_json,"
                " updated_at=excluded.updated_at",
                (segment_id, meeting_id, source_id, start_ms, end_ms, text, language, dumps(stt_metadata), now, now),
            )
            self.db.execute(
                "INSERT OR REPLACE INTO transcript_segment_revisions(segment_id,meeting_id,revision,text,speaker_label,"
                "origin,edited_at) VALUES (?,?,1,?,NULL,'stt',?)",
                (segment_id, meeting_id, text, now),
            )

    def delete_segment(self, segment_id: str) -> None:
        self.db.execute("DELETE FROM transcript_segments WHERE id=? AND is_final=0", (segment_id,))

    def delete_partials(self, meeting_id: str) -> int:
        """Partial rows whose audio will never be finalized (after stop or an interrupted session)."""
        return self.db.execute("DELETE FROM transcript_segments WHERE meeting_id=? AND is_final=0",
                               (meeting_id,)).rowcount

    def get_segment(self, meeting_id: str, segment_id: str) -> dict[str, Any] | None:
        return _d(
            self.db.one("SELECT * FROM transcript_segments WHERE id=? AND meeting_id=?", (segment_id, meeting_id))
        )

    def list_segments(
        self, meeting_id: str, *, after_ms: int | None = None, limit: int | None = None, include_partial: bool = False,
        cursor: tuple[int, str] | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM transcript_segments WHERE meeting_id=?"
        params: list[Any] = [meeting_id]
        if not include_partial:
            sql += " AND is_final=1"
        if after_ms is not None:
            sql += " AND start_ms >= ?"
            params.append(after_ms)
        if cursor is not None:
            sql += " AND (start_ms > ? OR (start_ms = ? AND id > ?))"
            params += [cursor[0], cursor[0], cursor[1]]
        sql += " ORDER BY start_ms, id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [dict(r) for r in self.db.query(sql, params)]

    def final_segments_in_range(self, meeting_id: str, start_ms: int, end_ms: int | None) -> list[dict[str, Any]]:
        """Final segments whose end lies in (start_ms, end_ms]."""
        sql = "SELECT * FROM transcript_segments WHERE meeting_id=? AND is_final=1 AND end_ms > ?"
        params: list[Any] = [meeting_id, start_ms]
        if end_ms is not None:
            sql += " AND end_ms <= ?"
            params.append(end_ms)
        sql += " ORDER BY start_ms, id"
        return [dict(r) for r in self.db.query(sql, params)]

    def final_segments_before(self, meeting_id: str, before_end_ms: int, limit: int) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM transcript_segments WHERE meeting_id=? AND is_final=1 AND end_ms <= ? "
            "ORDER BY end_ms DESC, id DESC LIMIT ?",
            (meeting_id, before_end_ms, limit),
        )
        return [dict(r) for r in reversed(rows)]

    def segments_by_ids(self, meeting_id: str, ids: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(ids), 500):
            batch = ids[i : i + 500]
            marks = ",".join("?" for _ in batch)
            rows = self.db.query(
                f"SELECT * FROM transcript_segments WHERE meeting_id=? AND id IN ({marks})", [meeting_id, *batch]
            )
            out.update({r["id"]: dict(r) for r in rows})
        return out

    def last_final_segment_end(self, meeting_id: str) -> int:
        return int(
            self.db.scalar("SELECT COALESCE(MAX(end_ms), 0) FROM transcript_segments WHERE meeting_id=? AND is_final=1",
                           (meeting_id,)) or 0
        )

    def edit_segment(
        self, meeting_id: str, segment_id: str, *, text: str | None, speaker_label: str | None, set_speaker: bool
    ) -> dict[str, Any] | None:
        with self.db.tx():
            seg = self.get_segment(meeting_id, segment_id)
            if seg is None:
                return None
            new_rev = int(seg["revision"]) + 1
            new_text = seg["text"] if text is None else text
            new_speaker = speaker_label if set_speaker else seg["speaker_label"]
            now = now_iso()
            if int(seg["revision"]) == 0:
                # Editing a partial promotes it to final so the user text is never replaced by STT.
                self.db.execute(
                    "INSERT OR IGNORE INTO transcript_segment_revisions(segment_id,meeting_id,revision,text,"
                    "speaker_label,origin,edited_at) VALUES (?,?,0,?,?,'stt',?)",
                    (segment_id, meeting_id, seg["text"], seg["speaker_label"], seg["updated_at"]),
                )
            self.db.execute(
                "UPDATE transcript_segments SET text=?, speaker_label=?, revision=?, is_final=1, updated_at=? "
                "WHERE id=?",
                (new_text, new_speaker, new_rev, now, segment_id),
            )
            self.db.execute(
                "INSERT INTO transcript_segment_revisions(segment_id,meeting_id,revision,text,speaker_label,origin,"
                "edited_at) VALUES (?,?,?,?,?,'user',?)",
                (segment_id, meeting_id, new_rev, new_text, new_speaker, now),
            )
            return self.get_segment(meeting_id, segment_id)

    def segment_history(self, segment_id: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT revision, text, speaker_label, edited_at, origin FROM transcript_segment_revisions "
            "WHERE segment_id=? ORDER BY revision",
            (segment_id,),
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ audio chunks / issues
    def insert_chunk(
        self, *, meeting_id: str, source_id: str, start_ms: int, end_ms: int, status: str,
        storage_path: str | None = None, sha256: str | None = None,
    ) -> str:
        cid = new_id()
        self.db.execute(
            "INSERT INTO audio_chunks(id,meeting_id,source_id,start_ms,end_ms,storage_path,sha256,status,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (cid, meeting_id, source_id, start_ms, end_ms, storage_path, sha256, status, now_iso()),
        )
        return cid

    def list_chunks(self, meeting_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query("SELECT * FROM audio_chunks WHERE meeting_id=? ORDER BY start_ms",
                                               (meeting_id,))]

    def insert_issue(
        self, *, meeting_id: str, kind: str, source_id: str | None = None, at_ms: int | None = None,
        length_ms: int | None = None, details: dict[str, Any] | None = None,
    ) -> str:
        iid = new_id()
        self.db.execute(
            "INSERT INTO meeting_issues(id,meeting_id,source_id,kind,at_ms,length_ms,details_json,created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (iid, meeting_id, source_id, kind, at_ms, length_ms, dumps(details or {}), now_iso()),
        )
        return iid

    def list_issues(self, meeting_id: str) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM meeting_issues WHERE meeting_id=? ORDER BY created_at, rowid",
                             (meeting_id,))
        out = []
        for r in rows:
            d = dict(r)
            d["details"] = loads(d.pop("details_json"), {})
            out.append(d)
        return out

    # ------------------------------------------------------------------ jobs
    def insert_job(
        self, *, meeting_id: str, trigger: str, input_hash: str, range_start_ms: int, range_end_ms: int,
        segment_ids: list[str], base_revision_id: str | None, max_attempts: int,
    ) -> tuple[dict[str, Any], bool]:
        """Insert a job; on UNIQUE(meeting_id,input_hash) return the existing one and ``deduplicated=True``."""
        jid = new_id()
        with self.db.tx():
            existing = self.db.one(
                "SELECT * FROM summary_jobs WHERE meeting_id=? AND input_hash=?", (meeting_id, input_hash)
            )
            if existing is not None:
                return dict(existing), True
            self.db.execute(
                "INSERT INTO summary_jobs(id,meeting_id,trigger,status,input_hash,range_start_ms,range_end_ms,"
                "segment_ids_json,base_revision_id,attempt,max_attempts,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (jid, meeting_id, trigger, "queued", input_hash, range_start_ms, range_end_ms, dumps(segment_ids),
                 base_revision_id, 0, max_attempts, now_iso()),
            )
        job = self.get_job(jid)
        assert job is not None
        return job, False

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return _d(self.db.one("SELECT * FROM summary_jobs WHERE id=?", (job_id,)))

    def list_jobs(self, meeting_id: str) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM summary_jobs WHERE meeting_id=? ORDER BY created_at, rowid", (meeting_id,))
        return [dict(r) for r in rows]

    def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE summary_jobs SET {cols} WHERE id=?", [*fields.values(), job_id])

    def claimed_cursor_ms(self, meeting_id: str) -> int:
        marks = ",".join("?" for _ in CLAIMING_JOB_STATUSES)
        return int(
            self.db.scalar(
                f"SELECT COALESCE(MAX(range_end_ms), 0) FROM summary_jobs WHERE meeting_id=? AND status IN ({marks})",
                (meeting_id, *CLAIMING_JOB_STATUSES),
            ) or 0
        )

    def latest_job(self, meeting_id: str, *, exclude_canceled: bool = True) -> dict[str, Any] | None:
        sql = "SELECT * FROM summary_jobs WHERE meeting_id=?"
        if exclude_canceled:
            sql += " AND status != 'canceled'"
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT 1"
        return _d(self.db.one(sql, (meeting_id,)))

    def count_active_jobs(self, meeting_id: str | None = None) -> int:
        marks = ",".join("?" for _ in ACTIVE_JOB_STATUSES)
        if meeting_id is None:
            return int(self.db.scalar(f"SELECT COUNT(*) FROM summary_jobs WHERE status IN ({marks})",
                                      ACTIVE_JOB_STATUSES) or 0)
        return int(
            self.db.scalar(f"SELECT COUNT(*) FROM summary_jobs WHERE meeting_id=? AND status IN ({marks})",
                           (meeting_id, *ACTIVE_JOB_STATUSES)) or 0
        )

    def runnable_jobs(self) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM summary_jobs WHERE status IN ('queued','retry_wait','running') ORDER BY created_at, rowid"
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ notes
    def insert_note_revision(
        self, *, revision_id: str, meeting_id: str, job_id: str | None, origin: str, status: str, payload: dict,
        schema_version: str, source_hash: str | None, supersedes_id: str | None,
    ) -> None:
        self.db.execute(
            "INSERT INTO note_revisions(id,meeting_id,job_id,origin,status,schema_version,payload_json,created_at,"
            "source_hash,supersedes_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (revision_id, meeting_id, job_id, origin, status, schema_version, dumps(payload), now_iso(), source_hash,
             supersedes_id),
        )

    def _note(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        d = dict(row)
        d["payload"] = loads(d.pop("payload_json"), {})
        return d

    def current_note(self, meeting_id: str) -> dict[str, Any] | None:
        return self._note(
            self.db.one(
                "SELECT * FROM note_revisions WHERE meeting_id=? AND status='applied' ORDER BY seq DESC LIMIT 1",
                (meeting_id,),
            )
        )

    def pending_conflict(self, meeting_id: str) -> dict[str, Any] | None:
        return self._note(
            self.db.one(
                "SELECT * FROM note_revisions WHERE meeting_id=? AND status='pending_conflict' "
                "ORDER BY seq DESC LIMIT 1",
                (meeting_id,),
            )
        )

    def get_note_revision(self, meeting_id: str, revision_id: str) -> dict[str, Any] | None:
        return self._note(
            self.db.one("SELECT * FROM note_revisions WHERE meeting_id=? AND id=?", (meeting_id, revision_id))
        )

    def list_note_revisions(self, meeting_id: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT seq,id,meeting_id,job_id,origin,status,schema_version,created_at,supersedes_id,source_hash "
            "FROM note_revisions WHERE meeting_id=? ORDER BY seq",
            (meeting_id,),
        )
        return [dict(r) for r in rows]

    def user_revisions_after(self, meeting_id: str, base_revision_id: str | None) -> int:
        base_seq = 0
        if base_revision_id:
            base_seq = int(self.db.scalar("SELECT seq FROM note_revisions WHERE id=?", (base_revision_id,)) or 0)
        return int(
            self.db.scalar(
                "SELECT COUNT(*) FROM note_revisions WHERE meeting_id=? AND origin='user' AND status='applied' "
                "AND seq>?",
                (meeting_id, base_seq),
            ) or 0
        )

    def set_note_status(self, revision_id: str, status: str, supersedes_id: str | None = None) -> None:
        if supersedes_id is None:
            self.db.execute("UPDATE note_revisions SET status=? WHERE id=?", (status, revision_id))
        else:
            self.db.execute(
                "UPDATE note_revisions SET status=?, supersedes_id=? WHERE id=?", (status, supersedes_id, revision_id)
            )

    def touch_note_seq(self, revision_id: str) -> None:
        """Move a revision to the head of the ordering (used when accepting a pending AI conflict)."""
        nxt = int(self.db.scalar("SELECT COALESCE(MAX(seq),0)+1 FROM note_revisions") or 1)
        self.db.execute("UPDATE note_revisions SET seq=? WHERE id=?", (nxt, revision_id))

    # ------------------------------------------------------------------ settings
    def get_setting(self, key: str) -> Any:
        row = self.db.one("SELECT value_json FROM settings WHERE key=?", (key,))
        return None if row is None else loads(row["value_json"])

    def put_setting(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO settings(key,value_json,updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
            (key, dumps(value), now_iso()),
        )

    # ------------------------------------------------------------------ idempotency
    def get_idempotent(self, scope: str, key: str) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM idempotency_keys WHERE scope=? AND key=?", (scope, key))
        if row is None:
            return None
        return {"status": row["response_status"], "body": loads(row["response_json"])}

    def put_idempotent(self, scope: str, key: str, status: int, body: Any, meeting_id: str | None) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO idempotency_keys(scope,key,response_status,response_json,meeting_id,created_at)"
            " VALUES (?,?,?,?,?,?)",
            (scope, key, status, dumps(body), meeting_id, now_iso()),
        )
