"""Transport security (spec §11, AC-08): loopback Host, Origin allow-list, CORS for allowed origins only,
bearer token, JSON content type on mutating requests, request ids, access logging without payloads."""

from __future__ import annotations

import hmac
import json
import logging
import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from ..errors import MESSAGES_JA
from ..logsetup import log, request_id_var
from ..util import new_id

logger = logging.getLogger("sekretaer.http")

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost"})
MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
ALLOW_HEADERS = "Authorization, Content-Type, Idempotency-Key"
ALLOW_METHODS = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
WS_PROTOCOL = "sekretaer.v1"


def token_matches(presented: str | None, token: str) -> bool:
    if not presented:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), token.encode("utf-8"))


def host_allowed(host_header: str | None) -> bool:
    if not host_header:
        return False
    host = host_header.strip()
    if host.startswith("["):
        return False
    return host.rsplit(":", 1)[0].lower() in ALLOWED_HOSTS


def ws_token(protocol_header: str | None) -> tuple[bool, str | None]:
    """Parse ``Sec-WebSocket-Protocol: sekretaer.v1, bearer.<token>``."""
    protos = [p.strip() for p in (protocol_header or "").split(",") if p.strip()]
    token = next((p[len("bearer."):] for p in protos if p.startswith("bearer.")), None)
    return WS_PROTOCOL in protos, token


class SecurityMiddleware:
    def __init__(self, app: Callable[..., Awaitable[None]], *, token_getter: Callable[[], str],
                 allowed_origins: list[str]) -> None:
        self.app = app
        self.token_getter = token_getter
        self.allowed_origins = set(allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)  # websocket auth is enforced by the endpoint before accept
            return
        rid = new_id()
        ctx_token = request_id_var.set(rid)
        scope.setdefault("state", {})["request_id"] = rid
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        method = scope["method"]
        origin = headers.get("origin")
        origin_ok = origin is not None and origin in self.allowed_origins
        status_holder = {"status": 0}
        started = time.perf_counter()

        async def send_wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                hdrs = list(message.get("headers", []))
                hdrs.append((b"x-request-id", rid.encode()))
                hdrs.append((b"cache-control", b"no-store"))
                if origin_ok:
                    hdrs.append((b"access-control-allow-origin", origin.encode("latin-1")))
                    hdrs.append((b"access-control-expose-headers", b"X-Request-Id"))
                    hdrs.append((b"vary", b"Origin"))
                message["headers"] = hdrs
            await send(message)

        try:
            if not host_allowed(headers.get("host")):
                await self._error(send_wrapped, rid, 403, "origin_forbidden", {"reason": "host"})
                return
            if origin is not None and not origin_ok:
                await self._error(send_wrapped, rid, 403, "origin_forbidden", {"reason": "origin"})
                return
            if method == "OPTIONS" and origin_ok and "access-control-request-method" in headers:
                await send_wrapped({"type": "http.response.start", "status": 204, "headers": [
                    (b"access-control-allow-methods", ALLOW_METHODS.encode()),
                    (b"access-control-allow-headers", ALLOW_HEADERS.encode()),
                    (b"access-control-max-age", b"600"),
                ]})
                await send_wrapped({"type": "http.response.body", "body": b""})
                return
            auth = headers.get("authorization", "")
            presented = auth[7:].strip() if auth[:7].lower() == "bearer " else None
            if not token_matches(presented, self.token_getter()):
                await self._error(send_wrapped, rid, 401, "unauthorized", {})
                return
            if method in MUTATING:
                ctype = headers.get("content-type", "").split(";")[0].strip().lower()
                if ctype != "application/json":
                    await self._error(send_wrapped, rid, 415, "invalid_request",
                                      {"reason": "content_type", "expected": "application/json"},
                                      "Content-Type: application/json が必要です。")
                    return
            await self.app(scope, receive, send_wrapped)
        finally:
            log(logger, logging.INFO, "request", method=method, path=scope.get("path"),
                status=status_holder["status"], durationMs=round((time.perf_counter() - started) * 1000, 1))
            request_id_var.reset(ctx_token)

    @staticmethod
    async def _error(send: Send, rid: str, status: int, code: str, details: dict[str, Any],
                     message: str | None = None) -> None:
        body = json.dumps({"requestId": rid, "error": {
            "code": code, "message": message or MESSAGES_JA.get(code, code), "details": details,
        }}, ensure_ascii=False).encode("utf-8")
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", b"application/json; charset=utf-8"), (b"content-length", str(len(body)).encode()),
        ]})
        await send({"type": "http.response.body", "body": body})
