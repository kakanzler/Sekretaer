"""AC-03 (silence / manual / final create persistent jobs while audio keeps being processed) and
AC-09 (gaps, device loss, STT backlog, DB write failure are warned about and persisted)."""

from __future__ import annotations

import sqlite3

from sekretaer.audio.synthetic import ArraySourceSpec
from sekretaer.pipeline.session import SessionTuning
from sekretaer.stt.base import UnavailableStt
from sekretaer.stt.fake import ScriptedStt

from .conftest import Harness, speech_audio, wait_for

MIC = [{"kind": "microphone", "deviceKey": "synthetic:mic"}]


def _add(h: Harness, pattern: list[tuple[str, float]], **spec: object) -> None:
    audio = speech_audio(pattern)
    h.add_source("synthetic:mic", lambda: ArraySourceSpec(samples=audio, **spec))  # type: ignore[arg-type]


def _segments(h: Harness, mid: str) -> list[dict]:
    return h.ok("GET", f"/meetings/{mid}/transcript?limit=1000")["items"]


# ---------------------------------------------------------------------------------------------- AC-03


def test_silence_breakpoint_creates_persistent_job_while_audio_continues(harness: Harness) -> None:
    harness.fake_cli.set(mode="sleep", sleep=1.5)
    pattern = [("speech", 6.0), ("silence", 0.6)] * 2 + [("speech", 6.0), ("silence", 2.5)]
    pattern += [("speech", 5.0), ("silence", 0.7)] * 8
    _add(harness, pattern, speed=15.0)
    m = harness.create_meeting(sources=MIC)
    mid = m["id"]
    assert harness.start(mid).status_code == 200
    job = wait_for(lambda: next((j for j in harness.jobs(mid) if j["trigger"] == "silence"), None),
                   msg="silence job")
    # Persisted row (not only an event): visible straight from SQLite with its snapshot range.
    row = harness.ctx.repo.get_job(job["id"])
    assert row is not None and row["trigger"] == "silence" and row["range_end_ms"] > 15_000
    assert any(e["event"] == "summary.queued" and e["data"]["jobId"] == job["id"] for e in harness.events())
    wait_for(lambda: harness.ok("GET", f"/jobs/{job['id']}")["status"] == "running", msg="job running")
    n_before = len(_segments(harness, mid))
    # The CLI is still busy (sleeping); transcription keeps going meanwhile.
    wait_for(lambda: len(_segments(harness, mid)) >= n_before + 2, timeout=10, msg="segments during job")
    assert harness.ok("GET", f"/jobs/{job['id']}")["status"] == "running"
    wait_for(lambda: harness.ok("GET", f"/jobs/{job['id']}")["status"] == "succeeded", msg="job done")
    assert harness.meeting(mid)["state"] == "recording"


def test_manual_trigger_creates_job_and_deduplicates(harness: Harness) -> None:
    harness.ctx.jobs.paused = True
    _add(harness, [("speech", 6.0), ("silence", 0.7)] * 4)
    m = harness.create_meeting(sources=MIC)
    mid = m["id"]
    harness.start(mid)
    wait_for(lambda: len(_segments(harness, mid)) >= 4, msg="segments")
    assert harness.jobs(mid) == []  # no silence breakpoint in this audio
    first = harness.ok("POST", f"/meetings/{mid}/summaries", {}, status=202)
    assert first["deduplicated"] is False
    again = harness.ok("POST", f"/meetings/{mid}/summaries", {}, status=202)
    assert again == {"jobId": first["jobId"], "deduplicated": True}
    jobs = harness.jobs(mid)
    assert len(jobs) == 1 and jobs[0]["trigger"] == "manual" and jobs[0]["status"] == "queued"
    assert harness.meeting(mid)["state"] == "recording"


def test_manual_trigger_requires_external_processing_consent(harness: Harness) -> None:
    _add(harness, [("speech", 3.0)])
    m = harness.create_meeting(sources=MIC, external=False)
    r = harness.req("POST", f"/meetings/{m['id']}/summaries", {})
    assert r.status_code == 409 and r.json()["error"]["code"] == "consent_required"
    m2 = harness.create_meeting(sources=MIC, summarization=False)
    r = harness.req("POST", f"/meetings/{m2['id']}/summaries", {})
    assert r.status_code == 409 and r.json()["error"]["details"]["summarizationEnabled"] is False


def test_final_job_after_stop(harness: Harness) -> None:
    _add(harness, [("speech", 5.0), ("silence", 0.6), ("speech", 4.0)], speed=10.0, hold_open=True)
    m = harness.create_meeting(sources=MIC)
    mid = m["id"]
    harness.start(mid)
    wait_for(lambda: len(_segments(harness, mid)) >= 1, msg="first segment")
    r = harness.req("POST", f"/meetings/{mid}/stop", {})
    assert r.status_code == 202 and r.json()["data"]["state"] == "finalizing"
    harness.wait_state(mid, "completed")
    jobs = harness.jobs(mid)
    assert [j["trigger"] for j in jobs] == ["final"]
    # The final job covers the speech that was still in flight when stop was pressed.
    segs = _segments(harness, mid)
    assert len(segs) == 2 and jobs[0]["rangeEndMs"] >= segs[-1]["endMs"]
    wait_for(lambda: harness.jobs(mid)[0]["status"] == "succeeded", msg="final job")
    states = [e["data"]["state"] for e in harness.events() if e["event"] == "meeting.state" and e["meetingId"] == mid]
    assert states[-2:] == ["finalizing", "completed"]


def test_no_jobs_without_consent_but_transcript_kept(harness: Harness) -> None:
    _add(harness, [("speech", 6.0), ("silence", 0.6)] * 3 + [("speech", 6.0), ("silence", 2.5)])
    m = harness.create_meeting(sources=MIC, external=False)
    mid = m["id"]
    harness.start(mid)
    wait_for(lambda: len(_segments(harness, mid)) >= 4, msg="segments")
    harness.req("POST", f"/meetings/{mid}/stop", {})
    harness.wait_state(mid, "completed")
    assert harness.jobs(mid) == [] and harness.fake_cli.calls() == []


# ---------------------------------------------------------------------------------------------- AC-09


def test_audio_gap_is_warned_and_persisted(harness: Harness) -> None:
    _add(harness, [("speech", 4.0), ("silence", 2.0)], drop_blocks={20, 21, 22})
    m = harness.create_meeting(sources=MIC)
    mid = m["id"]
    harness.start(mid)
    gap = wait_for(lambda: next((e for e in harness.events() if e["event"] == "audio.gap"), None), msg="gap")
    assert gap["data"]["gapMs"] == 300 and gap["data"]["atMs"] == 2000
    assert gap["data"]["reason"] == "input_discontinuity" and gap["meetingId"] == mid
    issues = harness.ctx.repo.list_issues(mid)
    assert any(i["kind"] == "audio_gap" and i["at_ms"] == 2000 and i["length_ms"] == 300 for i in issues)
    chunks = harness.ctx.repo.list_chunks(mid)
    assert any(c["status"] == "gap" and c["start_ms"] == 2000 and c["end_ms"] == 2300 for c in chunks)


def test_device_disconnect_warns_marks_degraded_and_records_gap(harness: Harness) -> None:
    _add(harness, [("speech", 3.0), ("silence", 4.0)], disconnect_at_block=10, disconnect_skip_blocks=20)
    m = harness.create_meeting(sources=MIC)
    mid = m["id"]
    harness.start(mid)
    gap = wait_for(lambda: next((e for e in harness.events() if e["event"] == "audio.gap"), None), msg="gap")
    assert gap["data"]["reason"] == "device_disconnected" and gap["data"]["gapMs"] == 2000
    warnings = [e for e in harness.events() if e["event"] == "warning"]
    assert any(w["data"]["code"] == "source_unavailable" for w in warnings)
    degraded_states = [e["data"]["degraded"] for e in harness.events() if e["event"] == "meeting.state"]
    assert any("source_unavailable" in d for d in degraded_states)
    wait_for(lambda: "source_unavailable" not in harness.meeting(mid)["degraded"], msg="restored")
    assert any(i["kind"] == "device_disconnected" for i in harness.ctx.repo.list_issues(mid))


def test_stt_backlog_warning_persisted_and_cleared(harness_factory) -> None:
    h = harness_factory(stt=ScriptedStt(delay_s=0.4),
                        tuning=SessionTuning(reconnect_delay_s=0.05, backlog_warn_ms=4_000, backlog_clear_ms=1_000))
    _add(h, [("speech", 1.5), ("silence", 0.7)] * 12)
    m = h.create_meeting(sources=MIC, external=False)
    mid = m["id"]
    h.start(mid)
    warn = wait_for(lambda: next((e for e in h.events() if e["event"] == "warning"
                                  and e["data"]["code"] == "stt_queue_backlog"), None), msg="backlog warning")
    assert warn["data"]["details"]["backlogMs"] > 4_000
    assert "stt_queue_backlog" in h.meeting(mid)["degraded"] or any(
        "stt_queue_backlog" in e["data"].get("degraded", []) for e in h.events() if e["event"] == "meeting.state")
    assert any(i["kind"] == "stt_queue_backlog" for i in h.ctx.repo.list_issues(mid))
    wait_for(lambda: len(_segments(h, mid)) == 12, timeout=30, msg="backlog drained")
    wait_for(lambda: "stt_queue_backlog" not in h.meeting(mid)["degraded"], msg="backlog cleared")


def test_db_write_failure_warns_and_halts_processing(harness: Harness, monkeypatch) -> None:
    _add(harness, [("speech", 3.0), ("silence", 1.0)] * 3)
    m = harness.create_meeting(sources=MIC)
    mid = m["id"]

    def broken(**_kw: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(harness.ctx.repo, "finalize_segment", broken)
    harness.start(mid)
    warn = wait_for(lambda: next((e for e in harness.events() if e["event"] == "warning"
                                  and e["data"]["code"] == "db_write_failed"), None), msg="db warning")
    assert warn["data"]["details"]["action"] == "stop_recording_suggested"
    assert "db_write_failed" in harness.meeting(mid)["degraded"]
    assert any(i["kind"] == "db_write_failed" for i in harness.ctx.repo.list_issues(mid))


def test_stt_model_unavailable_still_records(harness_factory) -> None:
    h = harness_factory(stt=UnavailableStt())
    _add(h, [("speech", 3.0), ("silence", 1.0)])
    m = h.create_meeting(sources=MIC)
    r = h.start(m["id"])
    assert r.status_code == 200
    data = h.meeting(m["id"])
    assert data["state"] == "recording" and "stt_model_unavailable" in data["degraded"]
    health = h.ok("GET", "/health")
    assert health["stt"]["state"] == "unavailable" and health["status"] in ("degraded", "starting")
    assert health["stt"]["targetDir"] and health["stt"]["approxSizeMb"]
