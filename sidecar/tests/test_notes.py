"""AC-06: user-edited notes are never silently overwritten by later AI output; conflicts are visible and
resolvable. Also user edits of notes/segments keep history."""

from __future__ import annotations

import copy

from sekretaer.util import new_id

from .conftest import Harness, wait_for
from .test_summarizer import TEXTS, seed, wait_job


def _add_segment(h: Harness, mid: str, start: int, text: str) -> str:
    src = h.meeting(mid)["sources"][0]["id"]
    sid = new_id()
    h.ctx.repo.finalize_segment(segment_id=sid, meeting_id=mid, source_id=src, start_ms=start, end_ms=start + 3000,
                                text=text, language="ja", stt_metadata={})
    return sid


def test_user_edit_during_job_creates_visible_conflict(harness: Harness) -> None:
    mid, ids = seed(harness, TEXTS[:3])
    j1 = harness.ok("POST", f"/meetings/{mid}/summaries", {})
    wait_job(harness, j1["jobId"], "succeeded")
    ai1 = harness.ok("GET", f"/meetings/{mid}/notes")["current"]

    harness.fake_cli.set(mode="sleep", sleep=2.0)
    _add_segment(harness, mid, 30_000, "次の議題は来月の展示会です。")
    j2 = harness.ok("POST", f"/meetings/{mid}/summaries", {})
    wait_job(harness, j2["jobId"], "running")

    edited = copy.deepcopy(ai1["note"])
    edited["decisions"][0]["text"] = "評価は来週金曜に実施（利用者が修正）"
    edited["decisions"][0]["status"] = "agreed"
    edited["actions"][0].update({"confirmed": True, "origin": "user", "assignee": "田中 一郎"})
    user = harness.ok("PUT", f"/meetings/{mid}/notes", {"baseRevisionId": ai1["id"], "note": edited})
    assert user["origin"] == "user" and user["supersedesId"] == ai1["id"]

    wait_job(harness, j2["jobId"], "succeeded")
    notes = harness.ok("GET", f"/meetings/{mid}/notes")
    assert notes["current"]["id"] == user["id"]  # not overwritten
    assert notes["current"]["note"]["decisions"][0]["text"] == "評価は来週金曜に実施（利用者が修正）"
    pending = notes["pendingConflict"]
    assert pending is not None and pending["origin"] == "ai" and pending["status"] == "pending_conflict"
    updated = [e for e in harness.events() if e["event"] == "summary.updated"]
    assert updated[-1]["data"]["conflict"] is True and updated[-1]["data"]["revisionId"] == pending["id"]
    revs = harness.ok("GET", f"/meetings/{mid}/notes/revisions")["items"]
    assert [r["origin"] for r in revs] == ["ai", "user", "ai"] and "note" not in revs[0]

    kept = harness.ok("POST", f"/meetings/{mid}/notes/conflict/resolve",
                      {"action": "keep_mine", "conflictRevisionId": pending["id"]})
    assert kept["id"] == user["id"]
    notes = harness.ok("GET", f"/meetings/{mid}/notes")
    assert notes["pendingConflict"] is None and notes["current"]["id"] == user["id"]
    again = harness.req("POST", f"/meetings/{mid}/notes/conflict/resolve",
                        {"action": "accept_ai", "conflictRevisionId": pending["id"]})
    assert again.status_code == 409

    # A later AI job builds on the user's revision: no conflict, and user-confirmed items stay intact.
    harness.fake_cli.set(mode="ok")
    _add_segment(harness, mid, 40_000, "展示会の担当は佐藤さんです。")
    j3 = harness.ok("POST", f"/meetings/{mid}/summaries", {})
    wait_job(harness, j3["jobId"], "succeeded")
    notes = harness.ok("GET", f"/meetings/{mid}/notes")
    assert notes["pendingConflict"] is None and notes["current"]["origin"] == "ai"
    kept_action = next(a for a in notes["current"]["note"]["actions"] if a["id"] == edited["actions"][0]["id"])
    assert kept_action == edited["actions"][0]


def test_accept_ai_resolution(harness: Harness) -> None:
    mid, _ = seed(harness, TEXTS[:3])
    j1 = harness.ok("POST", f"/meetings/{mid}/summaries", {})
    wait_job(harness, j1["jobId"], "succeeded")
    ai1 = harness.ok("GET", f"/meetings/{mid}/notes")["current"]
    harness.fake_cli.set(mode="sleep", sleep=1.5)
    _add_segment(harness, mid, 30_000, "追加の発話です。")
    j2 = harness.ok("POST", f"/meetings/{mid}/summaries", {})
    wait_job(harness, j2["jobId"], "running")
    edited = copy.deepcopy(ai1["note"])
    edited["cornell"]["summary"] = "利用者の要約"
    harness.ok("PUT", f"/meetings/{mid}/notes", {"baseRevisionId": ai1["id"], "note": edited})
    wait_job(harness, j2["jobId"], "succeeded")
    pending = wait_for(lambda: harness.ok("GET", f"/meetings/{mid}/notes")["pendingConflict"], msg="conflict")
    accepted = harness.ok("POST", f"/meetings/{mid}/notes/conflict/resolve",
                          {"action": "accept_ai", "conflictRevisionId": pending["id"]})
    assert accepted["id"] == pending["id"] and accepted["status"] == "applied"
    notes = harness.ok("GET", f"/meetings/{mid}/notes")
    assert notes["current"]["id"] == pending["id"] and notes["pendingConflict"] is None


def test_put_with_stale_base_is_409(harness: Harness) -> None:
    mid, _ = seed(harness, TEXTS[:2])
    j1 = harness.ok("POST", f"/meetings/{mid}/summaries", {})
    wait_job(harness, j1["jobId"], "succeeded")
    cur = harness.ok("GET", f"/meetings/{mid}/notes")["current"]
    harness.ok("PUT", f"/meetings/{mid}/notes", {"baseRevisionId": cur["id"], "note": cur["note"]})
    stale = harness.req("PUT", f"/meetings/{mid}/notes", {"baseRevisionId": cur["id"], "note": cur["note"]})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "conflict"
    assert stale.json()["error"]["details"]["currentRevisionId"] != cur["id"]


def test_put_validates_schema_and_evidence(harness: Harness) -> None:
    mid, ids = seed(harness, TEXTS[:2])
    bad = harness.req("PUT", f"/meetings/{mid}/notes", {"baseRevisionId": None, "note": {"cornell": {}}})
    assert bad.status_code == 400 and bad.json()["error"]["details"]["errors"]
    note = {
        "coverage": {"fromMs": 0, "toMs": 9000},
        "cornell": {"cues": [], "notes": [], "summary": "手書き"},
        "bullets": [],
        "decisions": [{"id": new_id(), "text": "決定", "status": "agreed", "certainty": 1,
                       "evidenceSegmentIds": [new_id()]}],
        "actions": [],
        "openQuestions": [],
    }
    r = harness.req("PUT", f"/meetings/{mid}/notes", {"baseRevisionId": None, "note": note})
    assert r.status_code == 400 and "missingSegmentIds" in r.json()["error"]["details"]
    note["decisions"][0]["evidenceSegmentIds"] = [ids[1]]
    ok = harness.ok("PUT", f"/meetings/{mid}/notes", {"baseRevisionId": None, "note": note})
    assert ok["note"]["meetingId"] == mid and ok["note"]["revisionId"] == ok["id"]


def test_segment_edit_keeps_history(harness: Harness) -> None:
    mid, ids = seed(harness, TEXTS[:2])
    seg = harness.ok("PATCH", f"/meetings/{mid}/transcript/{ids[0]}", {"text": "修正後のテキスト"})
    assert seg["revision"] == 2 and seg["text"] == "修正後のテキスト" and seg["isFinal"] is True
    seg = harness.ok("PATCH", f"/meetings/{mid}/transcript/{ids[0]}", {"speakerLabel": "司会"})
    assert seg["revision"] == 3 and seg["speakerLabel"] == "司会"
    assert seg["stt"] == {"avgLogprob": -0.3, "noSpeechProb": 0.01}
    hist = harness.ok("GET", f"/meetings/{mid}/transcript/{ids[0]}/history")["items"]
    assert [(h["revision"], h["origin"]) for h in hist] == [(1, "stt"), (2, "user"), (3, "user")]
    assert hist[0]["text"] == TEXTS[0]
    empty = harness.req("PATCH", f"/meetings/{mid}/transcript/{ids[0]}", {})
    assert empty.status_code == 400
    missing = harness.req("PATCH", f"/meetings/{mid}/transcript/{new_id()}", {"text": "x"})
    assert missing.status_code == 404
