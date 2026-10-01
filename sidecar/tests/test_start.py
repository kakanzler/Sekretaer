"""AC-01: recording cannot start without consent / with denied permission; reason + settings path."""

from __future__ import annotations

from sekretaer.audio.synthetic import ArraySourceSpec

from .conftest import Harness, speech_audio


def _mic(h: Harness, key: str = "synthetic:mic", permission: str = "granted", kind: str = "microphone") -> None:
    audio = speech_audio([("silence", 1.0)])
    h.add_source(key, lambda: ArraySourceSpec(samples=audio, hold_open=True, permission=permission), kind=kind)


def test_start_refused_without_recording_consent(harness: Harness) -> None:
    _mic(harness)
    m = harness.create_meeting(sources=[{"kind": "microphone", "deviceKey": "synthetic:mic"}], recording=False)
    assert m["consent"]["recording"] == "unconfirmed" and m["state"] == "preparing"
    r = harness.start(m["id"])
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "consent_required" and err["details"]["scope"] == "recording"
    assert err["details"]["settingsPath"] and err["message"]
    assert harness.meeting(m["id"])["state"] == "preparing"
    # denied explicitly -> still refused
    harness.ok("POST", f"/meetings/{m['id']}/consent", {"scope": "recording", "granted": False, "policyVersion": "v1"})
    r = harness.start(m["id"], key="k2")
    assert r.status_code == 409 and r.json()["error"]["details"]["state"] == "denied"
    # granting consent afterwards allows the start
    harness.ok("POST", f"/meetings/{m['id']}/consent", {"scope": "recording", "granted": True, "policyVersion": "v1"})
    r = harness.start(m["id"], key="k3")
    assert r.status_code == 200 and r.json()["data"]["state"] == "recording"
    rows = harness.ctx.repo.list_consent(m["id"])
    assert [(x["scope"], x["event_type"], x["policy_version"]) for x in rows if x["scope"] == "recording"] == [
        ("recording", "denied", "v1"), ("recording", "granted", "v1")]


def test_start_refused_with_denied_permission(harness: Harness) -> None:
    _mic(harness, permission="denied")
    m = harness.create_meeting(sources=[{"kind": "microphone", "deviceKey": "synthetic:mic"}])
    r = harness.start(m["id"])
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "permission_denied"
    assert err["details"]["sources"][0]["kind"] == "microphone" and err["details"]["settingsPath"]
    m2 = harness.meeting(m["id"])
    assert m2["state"] == "preparing" and m2["sources"][0]["permissionState"] == "denied"


def test_all_sources_unavailable(harness: Harness) -> None:
    m = harness.create_meeting(sources=[{"kind": "system", "deviceKey": "system:default"}])
    r = harness.start(m["id"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "source_unavailable"


def test_no_sources(harness: Harness) -> None:
    m = harness.create_meeting(sources=[])
    r = harness.start(m["id"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "source_unavailable"


def test_system_unavailable_microphone_only_meeting_allowed(harness: Harness) -> None:
    _mic(harness)
    m = harness.create_meeting(sources=[{"kind": "microphone", "deviceKey": "synthetic:mic"},
                                        {"kind": "system", "deviceKey": "system:default"}])
    r = harness.start(m["id"])
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["state"] == "recording" and "source_unavailable" in data["degraded"]
    kinds = {s["kind"]: s["permissionState"] for s in data["sources"]}
    assert kinds == {"microphone": "granted", "system": "unavailable"}
    assert any(e["event"] == "warning" and e["data"]["code"] == "source_unavailable" for e in harness.events())


def test_idempotency_key_required_and_replayed(harness: Harness) -> None:
    _mic(harness)
    m = harness.create_meeting(sources=[{"kind": "microphone", "deviceKey": "synthetic:mic"}])
    r = harness.req("POST", f"/meetings/{m['id']}/start")
    assert r.status_code == 400 and r.json()["error"]["details"]["header"] == "Idempotency-Key"
    a = harness.start(m["id"], key="same")
    b = harness.start(m["id"], key="same")
    assert a.status_code == b.status_code == 200
    assert a.json()["data"]["startedAt"] == b.json()["data"]["startedAt"]
    c = harness.start(m["id"], key="other")
    assert c.status_code == 409 and c.json()["error"]["code"] == "invalid_state"


def test_only_one_meeting_records_at_a_time(harness: Harness) -> None:
    _mic(harness)
    m1 = harness.create_meeting(sources=[{"kind": "microphone", "deviceKey": "synthetic:mic"}])
    m2 = harness.create_meeting(sources=[{"kind": "microphone", "deviceKey": "synthetic:mic"}])
    assert harness.start(m1["id"]).status_code == 200
    r = harness.start(m2["id"])
    assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == "another_meeting_recording"


def test_defaults_are_private(harness: Harness) -> None:
    r = harness.ok("POST", "/meetings", {"title": "既定値", "language": "auto", "sources": []})
    assert r["settings"] == {"summarizationEnabled": False, "retainAudio": False, "audioRetentionDays": 7}
    assert r["consent"] == {"recording": "unconfirmed", "externalProcessing": "unconfirmed"}
    s = harness.ok("GET", "/settings")
    assert s["privacy"]["defaultRetainAudio"] is False
