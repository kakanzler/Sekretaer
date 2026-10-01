"""AC-07: deleting a meeting removes app-managed audio, notes, DB rows, temp files and exports and reports
success/failure per item. Also covers the encrypted audio archive and retention refusal."""

from __future__ import annotations

from pathlib import Path

from sekretaer.audio.synthetic import ArraySourceSpec
from sekretaer.privacy.crypto import MemorySecretStore
from sekretaer.privacy.deletion import MEETING_TABLES

from .conftest import Harness, speech_audio, wait_for

MIC = [{"kind": "microphone", "deviceKey": "synthetic:mic"}]
MARKER = "削除確認用の固有テキスト"


def _record_meeting(h: Harness, retain: bool) -> str:
    audio = speech_audio([("speech", 6.0), ("silence", 0.7)] * 5 + [("speech", 5.0), ("silence", 2.0)])
    h.add_source("synthetic:mic", lambda: ArraySourceSpec(samples=audio))
    h.stt.script = [MARKER + f" {i}。" for i in range(10)]
    m = h.create_meeting(sources=MIC, retain=retain)
    mid = m["id"]
    h.start(mid)
    wait_for(lambda: len(h.ok("GET", f"/meetings/{mid}/transcript")["items"]) >= 6, msg="segments")
    h.req("POST", f"/meetings/{mid}/stop", {})
    h.wait_state(mid, "completed")
    wait_for(lambda: all(j["status"] == "succeeded" for j in h.jobs(mid)), msg="jobs")
    return mid


def test_encrypted_archive_written_only_when_retained(harness: Harness) -> None:
    mid = _record_meeting(harness, retain=True)
    chunks = harness.ctx.repo.list_chunks(mid)
    stored = [c for c in chunks if c["status"] == "stored"]
    assert len(stored) >= 2 and all(c["storage_path"] and c["sha256"] for c in stored)
    blob = Path(stored[0]["storage_path"]).read_bytes()
    assert blob[:4] == b"SKA1"
    pcm = harness.ctx.cipher.decrypt(mid, blob)
    assert len(pcm) == 30 * 16000 * 2  # first chunk: 30 s of 16 kHz int16
    assert harness.secrets.values  # per-meeting key lives in the credential store, not the DB
    db_bytes = harness.config.db_path.read_bytes()
    assert harness.secrets.values[f"meeting-audio:{mid}"] not in db_bytes


def test_no_audio_written_by_default(harness: Harness) -> None:
    mid = _record_meeting(harness, retain=False)
    chunks = harness.ctx.repo.list_chunks(mid)
    assert chunks and all(c["status"] == "discarded" and c["storage_path"] is None and c["sha256"] is None
                          for c in chunks)
    assert not harness.config.meeting_audio_dir(mid).exists()


def test_delete_removes_everything_and_reports(harness: Harness) -> None:
    mid = _record_meeting(harness, retain=True)
    other = harness.create_meeting(sources=[], title="残す会議")
    harness.ok("GET", f"/meetings/{mid}/export?format=markdown")
    harness.ok("GET", f"/meetings/{mid}/export?format=json")
    tmp_dir = harness.config.meeting_tmp_dir(mid)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    (tmp_dir / "partial.wav").write_bytes(b"\0" * 64)
    audio_files = list(harness.config.meeting_audio_dir(mid).glob("*"))
    assert audio_files

    result = harness.ok("DELETE", f"/meetings/{mid}")
    assert result["status"] == "deleted" and result["failed"] == []
    assert result["deleted"]["audioFiles"] == len(audio_files)
    assert result["deleted"]["tempFiles"] == 1 and result["deleted"]["exports"] == 2
    assert result["deleted"]["dbRecords"] > 10

    for d in (harness.config.meeting_audio_dir(mid), tmp_dir, harness.config.meeting_exports_dir(mid)):
        assert not d.exists()
    db = harness.ctx.repo.db
    for table in MEETING_TABLES:
        n = db.scalar(f"SELECT COUNT(*) FROM {table} WHERE meeting_id=? AND NOT (event='meeting.state')", (mid,)) \
            if table == "events" else db.scalar(f"SELECT COUNT(*) FROM {table} WHERE meeting_id=?", (mid,))
        assert n == 0, table
    row = harness.ctx.repo.get_meeting(mid)
    assert row["state"] == "deleted" and row["title"] == ""
    assert f"meeting-audio:{mid}" not in harness.secrets.values
    assert harness.req("GET", f"/meetings/{mid}").status_code == 404
    listed = [m["id"] for m in harness.ok("GET", "/meetings")["items"]]
    assert mid not in listed and other["id"] in listed
    # Content is gone from the database file itself (secure_delete + WAL checkpoint).
    wal = Path(str(harness.config.db_path) + "-wal")
    assert MARKER.encode("utf-8") not in harness.config.db_path.read_bytes()
    assert not wal.exists() or MARKER.encode("utf-8") not in wal.read_bytes()
    last = [e for e in harness.events() if e["meetingId"] == mid][-1]
    assert last["event"] == "meeting.state" and last["data"]["state"] == "deleted"


def test_partial_failure_reports_delete_incomplete(harness: Harness, monkeypatch) -> None:
    mid = _record_meeting(harness, retain=True)
    victim = sorted(harness.config.meeting_audio_dir(mid).glob("*"))[0]
    real_unlink = Path.unlink

    def flaky_unlink(self: Path, missing_ok: bool = False) -> None:
        if self == victim:
            raise PermissionError("locked")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    result = harness.ok("DELETE", f"/meetings/{mid}")
    assert result["status"] == "delete_incomplete"
    kinds = {(f["kind"], Path(f["path"]).name) for f in result["failed"]}
    assert ("audio", victim.name) in kinds
    assert all(f["reason"] for f in result["failed"])
    assert harness.ctx.repo.get_meeting(mid)["state"] == "deleting"
    monkeypatch.setattr(Path, "unlink", real_unlink)
    retry = harness.ok("DELETE", f"/meetings/{mid}")
    assert retry["status"] == "deleted" and retry["deleted"]["audioFiles"] == 1


def test_delete_while_recording_and_summarizing(harness: Harness) -> None:
    harness.fake_cli.set(mode="sleep", sleep=30)
    audio = speech_audio([("speech", 6.0), ("silence", 0.6)] * 3)
    harness.add_source("synthetic:mic", lambda: ArraySourceSpec(samples=audio, speed=2.0, hold_open=True))
    m = harness.create_meeting(sources=MIC)
    mid = m["id"]
    harness.start(mid)
    wait_for(lambda: len(harness.ok("GET", f"/meetings/{mid}/transcript")["items"]) >= 1, msg="segment")
    job = harness.ok("POST", f"/meetings/{mid}/summaries", {})
    wait_for(lambda: harness.ctx.repo.get_job(job["jobId"])["status"] == "running", msg="running")
    result = harness.ok("DELETE", f"/meetings/{mid}")
    assert result["status"] == "deleted"
    assert mid not in harness.ctx.meetings.sessions
    wait_for(lambda: harness.ctx.cli._procs == {}, msg="claude process killed")
    assert harness.req("GET", f"/jobs/{job['jobId']}").status_code == 404


def test_retention_refused_without_secure_storage(harness_factory) -> None:
    h = harness_factory(secret_store=MemorySecretStore(usable=False))
    r = h.req("POST", "/meetings", {"title": "t", "language": "ja", "sources": [],
                                    "settings": {"retainAudio": True}})
    assert r.status_code == 400
    assert r.json()["error"]["details"]["reason"] == "secure_storage_unavailable"
    m = h.create_meeting(sources=[])
    r = h.req("PATCH", f"/meetings/{m['id']}", {"settings": {"retainAudio": True}})
    assert r.status_code == 400
    assert h.ok("GET", "/privacy")["audioArchive"]["available"] is False
