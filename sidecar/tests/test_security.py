"""AC-08: loopback only, bearer token, Origin allow-list, CSRF content type, WS subprotocol auth."""

from __future__ import annotations

import pytest
from starlette.websockets import WebSocketDisconnect

from sekretaer.__main__ import _bind_loopback
from sekretaer.config import AppConfig

from .conftest import ORIGIN, Harness

WS = "ws://127.0.0.1"


def test_bind_is_loopback_only(tmp_path) -> None:
    sock = _bind_loopback()
    try:
        host, port = sock.getsockname()
        assert host == "127.0.0.1" and port > 0
    finally:
        sock.close()
    assert AppConfig(data_dir=tmp_path).host == "127.0.0.1"


def test_missing_and_invalid_token_rejected(harness: Harness) -> None:
    r = harness.client.get("/api/v1/health")
    assert r.status_code == 401
    body = r.json()
    assert body["error"]["code"] == "unauthorized" and body["requestId"] == r.headers["x-request-id"]
    r = harness.client.get("/api/v1/health", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
    r = harness.client.get("/api/v1/meetings", headers={"Authorization": harness.ctx.token})  # no Bearer prefix
    assert r.status_code == 401
    ok = harness.req("GET", "/health")
    assert ok.status_code == 200 and ok.json()["data"]["apiVersion"] == "1"
    assert ok.json()["requestId"] == ok.headers["x-request-id"]


def test_origin_checks(harness: Harness) -> None:
    bad = harness.req("GET", "/health", headers={"Origin": "https://evil.example"})
    assert bad.status_code == 403 and bad.json()["error"]["code"] == "origin_forbidden"
    null_origin = harness.req("GET", "/health", headers={"Origin": "null"})
    assert null_origin.status_code == 403
    good = harness.req("GET", "/health", headers={"Origin": ORIGIN})
    assert good.status_code == 200
    assert good.headers["access-control-allow-origin"] == ORIGIN
    # CORS preflight: approved only for allowed origins, never for unknown ones.
    pre = harness.client.options("/api/v1/meetings", headers={
        "Origin": ORIGIN, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type"})
    assert pre.status_code == 204 and "Authorization" in pre.headers["access-control-allow-headers"]
    pre_bad = harness.client.options("/api/v1/meetings", headers={
        "Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST"})
    assert pre_bad.status_code == 403  # dev origin only in --dev


def test_dev_mode_allows_localhost_3000(tmp_path) -> None:
    cfg = AppConfig(data_dir=tmp_path, dev=True)
    assert "http://localhost:3000" in cfg.allowed_origins
    assert "tauri://localhost" in cfg.allowed_origins


def test_host_header_must_be_loopback(harness: Harness) -> None:
    r = harness.req("GET", "/health", headers={"Host": "attacker.example"})
    assert r.status_code == 403 and r.json()["error"]["details"]["reason"] == "host"


def test_mutating_requests_require_json(harness: Harness) -> None:
    r = harness.client.post("/api/v1/meetings", content=b"title=x",
                            headers={"Authorization": f"Bearer {harness.ctx.token}",
                                     "Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 415 and r.json()["error"]["code"] == "invalid_request"


def test_validation_errors_do_not_echo_values(harness: Harness) -> None:
    r = harness.req("POST", "/meetings", {"title": "", "language": "xx", "secret": "機密テキスト"})
    assert r.status_code == 400
    assert "機密テキスト" not in r.text
    assert r.json()["error"]["code"] == "invalid_request"


def test_unknown_route_is_enveloped(harness: Harness) -> None:
    r = harness.req("GET", "/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_websocket_requires_subprotocol_token(harness: Harness) -> None:
    c = harness.client
    good = ["sekretaer.v1", "bearer." + harness.ctx.token]
    with c.websocket_connect(WS + "/api/v1/events", subprotocols=good) as ws:  # control: valid handshake
        assert ws.accepted_subprotocol == "sekretaer.v1"
    with pytest.raises(WebSocketDisconnect):  # non-loopback Host
        with c.websocket_connect("ws://evil.example/api/v1/events", subprotocols=good):
            pass
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect(WS + "/api/v1/events"):
            pass
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect(WS + "/api/v1/events", subprotocols=["sekretaer.v1"]):
            pass
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect(WS + "/api/v1/events", subprotocols=["sekretaer.v1", "bearer.wrong"]):
            pass
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect(WS + "/api/v1/events", subprotocols=["bearer." + harness.ctx.token]):
            pass
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect(WS + "/api/v1/events", subprotocols=["sekretaer.v1", "bearer." + harness.ctx.token],
                                 headers={"Origin": "https://evil.example"}):
            pass
    # token in the URL is not accepted
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect(WS + f"/api/v1/events?token={harness.ctx.token}"):
            pass


def test_websocket_replay_and_live(harness: Harness) -> None:
    m = harness.create_meeting(sources=[])
    with harness.client.websocket_connect(
        WS + "/api/v1/events?cursor=0", subprotocols=["sekretaer.v1", "bearer." + harness.ctx.token],
        headers={"Origin": ORIGIN},
    ) as ws:
        assert ws.accepted_subprotocol == "sekretaer.v1"
        first = ws.receive_json()
        assert first["event"] == "meeting.state" and first["meetingId"] == m["id"] and first["seq"] >= 1
        harness.ok("PATCH", f"/meetings/{m['id']}", {"settings": {"summarizationEnabled": False}})
        live = ws.receive_json()
        assert live["event"] == "privacy.state" and live["seq"] > first["seq"]
    # reconnect with cursor: nothing before the cursor is replayed
    with harness.client.websocket_connect(
        WS + f"/api/v1/events?cursor={live['seq']}", subprotocols=["sekretaer.v1", "bearer." + harness.ctx.token],
    ) as ws:
        harness.ok("PATCH", f"/meetings/{m['id']}", {"title": "改名"})
        harness.ok("PATCH", f"/meetings/{m['id']}", {"settings": {"summarizationEnabled": True}})
        ev = ws.receive_json()
        assert ev["seq"] > live["seq"] and ev["event"] == "privacy.state"


def test_logs_never_contain_token(harness: Harness, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")
    harness.req("GET", "/health")
    harness.client.get("/api/v1/health", headers={"Authorization": "Bearer guess"})
    assert harness.ctx.token not in caplog.text


def test_settings_roundtrip_and_validation(harness: Harness) -> None:
    s = harness.ok("GET", "/settings")
    assert s["summarizer"]["timeoutSec"] == 120
    assert s["stt"] == {"model": "small", "computeType": "int8", "device": "cpu"}
    out = harness.ok("PUT", "/settings", {"vad": {"sensitivity": "high"}, "summarizer": {"timeoutSec": 60}})
    assert out["vad"]["sensitivity"] == "high" and out["summarizer"]["timeoutSec"] == 60
    assert out["summarizer"]["enabled"] is True  # untouched fields keep their values
    bad = harness.req("PUT", "/settings", {"summarizer": {"cliPath": "relative/claude"}})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_request"
    bad = harness.req("PUT", "/settings", {"summarizer": {"apiKey": "x"}})  # secrets are not settings
    assert bad.status_code == 400
    disabled = harness.ok("PUT", "/settings", {"summarizer": {"enabled": False}})
    assert disabled["summarizer"]["enabled"] is False
    assert harness.ok("GET", "/health")["summarizer"]["state"] == "disabled"
    assert harness.ctx.repo.get_setting("app")["vad"]["sensitivity"] == "high"  # persisted
