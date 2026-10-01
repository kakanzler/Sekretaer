"""AC-10 (simulated): synthetic source + scripted STT + fake CLI drive a time-compressed 30-minute meeting
through recording -> stop -> final summary -> completed -> Markdown and JSON export."""

from __future__ import annotations

import json

from sekretaer.audio.synthetic import ArraySourceSpec
from sekretaer.summarization.schema import collect_evidence_ids, event_validator, note_errors

from .conftest import Harness, speech_audio, wait_for


def _thirty_minute_pattern() -> list[tuple[str, float]]:
    pattern: list[tuple[str, float]] = []
    total = 0.0
    i = 0
    while total < 30 * 60:
        speech = 5.0 + (i % 4) * 1.5
        pause = 0.8 if i % 3 else 2.4  # some pauses too short to be breakpoints
        pattern += [("speech", speech), ("silence", pause)]
        total += speech + pause
        i += 1
    return pattern


def test_ac10_full_meeting_to_export(harness: Harness) -> None:
    audio = speech_audio(_thirty_minute_pattern())
    harness.add_source("synthetic:mic", lambda: ArraySourceSpec(samples=audio, hold_open=True))
    m = harness.create_meeting(sources=[{"kind": "microphone", "deviceKey": "synthetic:mic"}])
    mid = m["id"]
    r = harness.start(mid)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["state"] == "recording"

    # All 30 minutes of audio are processed (faster than realtime) and silence breakpoints create jobs.
    wait_for(lambda: len(harness.ok("GET", f"/meetings/{mid}/transcript?limit=1000")["items"]) >= 150,
             timeout=120, msg="transcript of the whole meeting")
    wait_for(lambda: any(j["trigger"] == "silence" for j in harness.jobs(mid)), msg="silence job")

    stopped = harness.req("POST", f"/meetings/{mid}/stop")
    assert stopped.status_code == 202
    assert stopped.json()["data"]["state"] in ("finalizing", "completed")
    done = harness.wait_state(mid, "completed", timeout=60)
    assert done["endedAt"] is not None

    # Every job finishes; a final job exists when anything remained unsummarized.
    wait_for(lambda: all(j["status"] == "succeeded" for j in harness.jobs(mid)), timeout=60, msg="jobs succeeded")
    jobs = harness.jobs(mid)
    triggers = {j["trigger"] for j in jobs}
    assert "silence" in triggers

    notes = harness.ok("GET", f"/meetings/{mid}/notes")
    current = notes["current"]
    assert current is not None and notes["pendingConflict"] is None
    note = current["note"]
    assert note_errors(note) == []
    segments = harness.ok("GET", f"/meetings/{mid}/transcript?limit=1000")["items"]
    seg_ids = {s["id"] for s in segments}
    assert collect_evidence_ids(note) <= seg_ids
    assert note["coverage"]["toMs"] >= segments[-1]["endMs"] - 1
    assert note["decisions"] and all(d["evidenceSegmentIds"] for d in note["decisions"])
    assert all(a["confirmed"] is False and a["origin"] == "ai" for a in note["actions"])
    assert any(a["dueDate"] == "来週水曜" for a in note["actions"])  # relative date kept verbatim

    md = harness.ok("GET", f"/meetings/{mid}/export?format=markdown")
    assert md["filename"].endswith(".md") and md["content"].startswith("# 定例会議")
    assert "## 決定事項" in md["content"] and "## 逐語記録" in md["content"]
    js = harness.ok("GET", f"/meetings/{mid}/export?format=json")
    doc = json.loads(js["content"])
    assert doc["schemaVersion"] == "1.0" and doc["note"]["revisionId"] == current["id"]
    assert len(doc["transcript"]) == len(segments)
    assert (harness.config.meeting_exports_dir(mid) / js["filename"]).is_file()

    # Every persisted event matches the WebSocket envelope schema.
    validator = event_validator()
    events = harness.events()
    for ev in events:
        errors = list(validator.iter_errors(ev))
        assert not errors, (ev["event"], [e.message for e in errors])
    kinds = {e["event"] for e in events}
    assert {"meeting.state", "transcript.final", "summary.queued", "summary.updated"} <= kinds
    assert harness.meeting(mid)["processing"]["pendingJobs"] == 0
