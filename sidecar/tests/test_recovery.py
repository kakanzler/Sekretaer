"""Restart after an abnormal end (spec §9 アプリ異常終了, appendix A): recording is never resumed
automatically; the meeting is marked ``interrupted`` and finalized only on ``POST /recover``."""

from __future__ import annotations

from sekretaer.audio.synthetic import ArraySourceSpec

from .conftest import Harness, speech_audio, wait_for

MIC = [{"kind": "microphone", "deviceKey": "synthetic:mic"}]


def test_interrupted_meeting_requires_explicit_recover(tmp_path) -> None:
    audio = speech_audio([("speech", 5.0), ("silence", 0.7)] * 3)
    first = Harness(tmp_path)
    first.add_source("synthetic:mic", lambda: ArraySourceSpec(samples=audio, speed=4.0, hold_open=True))
    with first:
        m = first.create_meeting(sources=MIC)
        mid = m["id"]
        assert first.start(mid).status_code == 200
        wait_for(lambda: len(first.ok("GET", f"/meetings/{mid}/transcript")["items"]) >= 1, msg="segment")
        first.ctx.jobs.paused = True
    # process gone without /stop: state stays "recording" in the database
    second = Harness(tmp_path)
    with second:
        got = second.meeting(mid)
        assert got["state"] == "recording" and "interrupted" in got["degraded"]
        assert mid not in second.ctx.meetings.sessions  # capture was not resumed
        r = second.req("POST", f"/meetings/{mid}/recover", {"action": "resume"})
        assert r.status_code == 400
        rec = second.ok("POST", f"/meetings/{mid}/recover", {"action": "finalize"})
        assert rec["state"] == "finalizing"
        done = second.wait_state(mid, "completed")
        assert "interrupted" not in done["degraded"]
        jobs = second.jobs(mid)
        assert jobs and jobs[-1]["trigger"] == "final"
        wait_for(lambda: second.jobs(mid)[-1]["status"] == "succeeded", msg="final job")
        again = second.req("POST", f"/meetings/{mid}/recover", {"action": "finalize"})
        assert again.status_code == 409
