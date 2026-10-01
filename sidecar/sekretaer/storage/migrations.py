"""Numbered, forward-only schema migrations (spec §7, §12 保守性)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .db import Database

MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "initial",
        """
CREATE TABLE meetings (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    language TEXT NOT NULL CHECK (language IN ('auto','ja','en')),
    state TEXT NOT NULL CHECK (state IN
        ('preparing','recording','finalizing','completed','error','deleting','deleted')),
    degraded_json TEXT NOT NULL DEFAULT '[]',
    started_at TEXT,
    ended_at TEXT,
    timezone TEXT NOT NULL,
    settings_json TEXT NOT NULL,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_meetings_created ON meetings(created_at);

CREATE TABLE audio_sources (
    id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('microphone','system')),
    device_key TEXT NOT NULL,
    sample_rate INTEGER,
    channels INTEGER,
    permission_state TEXT NOT NULL DEFAULT 'unknown',
    offset_ms INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_sources_meeting ON audio_sources(meeting_id);

CREATE TABLE audio_chunks (
    id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    storage_path TEXT,
    sha256 TEXT,
    status TEXT NOT NULL CHECK (status IN ('stored','discarded','gap','purged')),
    created_at TEXT NOT NULL
);
CREATE INDEX idx_chunks_meeting ON audio_chunks(meeting_id, start_ms);

CREATE TABLE transcript_segments (
    id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    text TEXT NOT NULL,
    language TEXT,
    is_final INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 0,
    speaker_label TEXT,
    stt_metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_segments_meeting ON transcript_segments(meeting_id, start_ms);

CREATE TABLE transcript_segment_revisions (
    segment_id TEXT NOT NULL REFERENCES transcript_segments(id) ON DELETE CASCADE,
    meeting_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    text TEXT NOT NULL,
    speaker_label TEXT,
    origin TEXT NOT NULL CHECK (origin IN ('stt','user')),
    edited_at TEXT NOT NULL,
    PRIMARY KEY (segment_id, revision)
);

CREATE TABLE summary_jobs (
    id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    trigger TEXT NOT NULL CHECK (trigger IN ('silence','long_speech','manual','final')),
    status TEXT NOT NULL CHECK (status IN
        ('queued','running','retry_wait','succeeded','failed','canceled')),
    input_hash TEXT NOT NULL,
    range_start_ms INTEGER NOT NULL,
    range_end_ms INTEGER NOT NULL,
    segment_ids_json TEXT NOT NULL DEFAULT '[]',
    base_revision_id TEXT,
    result_revision_id TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error_code TEXT,
    next_retry_at TEXT,
    UNIQUE (meeting_id, input_hash)
);
CREATE INDEX idx_jobs_status ON summary_jobs(status, created_at);

CREATE TABLE note_revisions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    job_id TEXT,
    origin TEXT NOT NULL CHECK (origin IN ('ai','user')),
    status TEXT NOT NULL CHECK (status IN ('applied','pending_conflict','rejected')),
    schema_version TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    source_hash TEXT,
    supersedes_id TEXT
);
CREATE INDEX idx_notes_meeting ON note_revisions(meeting_id, seq);

CREATE TABLE consent_events (
    id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL CHECK (event_type IN ('granted','denied','withdrawn')),
    scope TEXT NOT NULL CHECK (scope IN ('recording','external_processing')),
    granted INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    policy_version TEXT NOT NULL
);
CREATE INDEX idx_consent_meeting ON consent_events(meeting_id, recorded_at);

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE events (
    seq INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL,
    event TEXT NOT NULL,
    meeting_id TEXT,
    occurred_at TEXT NOT NULL,
    data_json TEXT NOT NULL
);
CREATE INDEX idx_events_meeting ON events(meeting_id);

CREATE TABLE idempotency_keys (
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    response_status INTEGER NOT NULL,
    response_json TEXT NOT NULL,
    meeting_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);

CREATE TABLE meeting_issues (
    id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    source_id TEXT,
    kind TEXT NOT NULL,
    at_ms INTEGER,
    length_ms INTEGER,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX idx_issues_meeting ON meeting_issues(meeting_id, created_at);
""",
    ),
]


def apply_migrations(db: Database) -> None:
    conn = db.conn
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    for version, name, sql in MIGRATIONS:
        if version in applied:
            continue
        now = datetime.now(UTC).isoformat()
        # executescript commits implicitly, so the whole migration + bookkeeping is one script.
        try:
            conn.executescript(
                "BEGIN;\n"
                + sql
                + f"\nINSERT INTO schema_migrations(version, name, applied_at) VALUES ({int(version)}, "
                + f"'{name}', '{now}');\nCOMMIT;"
            )
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


def current_version(db: Database) -> int:
    return int(db.scalar("SELECT COALESCE(MAX(version), 0) FROM schema_migrations") or 0)
