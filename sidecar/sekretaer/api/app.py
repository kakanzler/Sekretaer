"""FastAPI application: REST resources and the event WebSocket under /api/v1 (contracts/api-v1.md)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI, Header, Query, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.websockets import WebSocketDisconnect, WebSocketState

from .. import API_VERSION, __version__
from ..app_context import AppContext
from ..errors import ApiError
from ..jobs.queue import job_to_api
from ..logsetup import log, request_id_var
from ..settings import SettingsPatch
from ..util import new_id
from . import models as M
from .security import WS_PROTOCOL, SecurityMiddleware, host_allowed, token_matches, ws_token

logger = logging.getLogger("sekretaer.api")
PREFIX = "/api/v1"


def _rid() -> str:
    return request_id_var.get() or new_id()


def ok(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse({"requestId": _rid(), "data": data}, status_code=status)


def err(status: int, code: str, message: str, details: dict[str, Any] | None = None) -> JSONResponse:
    return JSONResponse({"requestId": _rid(), "error": {"code": code, "message": message, "details": details or {}}},
                        status_code=status)


def _doc(model: Any, *codes: int) -> dict[int | str, dict[str, Any]]:
    out: dict[int | str, dict[str, Any]] = {codes[0] if codes else 200: {"model": M.Envelope[model]}}
    for c in (400, 401, 403, 404, 409):
        out.setdefault(c, {"model": M.ErrorEnvelope})
    return out


def create_app(ctx: AppContext) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await ctx.startup()
        try:
            yield
        finally:
            await ctx.shutdown()

    app = FastAPI(
        title="Sekretär sidecar API",
        version=API_VERSION,
        summary=f"Loopback API of the Sekretär Python sidecar {__version__}. See contracts/api-v1.md.",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,  # never served unauthenticated; exported by scripts/export-openapi.py
        lifespan=lifespan,
    )
    app.state.ctx = ctx

    @app.exception_handler(ApiError)
    async def _api_error(_req: Request, exc: ApiError) -> JSONResponse:
        return err(exc.status, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation(_req: Request, exc: RequestValidationError) -> JSONResponse:
        # Field locations and error types only; never echo submitted values (may contain transcript text).
        errors = [{"loc": [str(p) for p in e.get("loc", [])], "type": e.get("type")} for e in exc.errors()][:20]
        return err(400, "invalid_request", "要求の形式が正しくありません。", {"errors": errors})

    @app.exception_handler(StarletteHTTPException)
    async def _http(_req: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            return err(404, "not_found", "対象が見つかりません。")
        if exc.status_code == 405:
            return err(405, "invalid_request", "このメソッドは使用できません。", {"reason": "method_not_allowed"})
        return err(exc.status_code, "invalid_request", "要求を処理できません。")

    @app.exception_handler(Exception)
    async def _unhandled(_req: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled error", exc_info=(type(exc), exc, None))
        return err(500, "internal_error", "内部エラーが発生しました。")

    r = APIRouter(prefix=PREFIX)

    # ------------------------------------------------------------------ system
    @r.get("/health", responses=_doc(M.HealthOut))
    async def health() -> JSONResponse:
        return ok(ctx.health())

    @r.get("/devices", responses=_doc(M.DeviceList))
    async def devices() -> JSONResponse:
        items = await asyncio.to_thread(ctx.backend.list_devices)
        return ok({"devices": [d.to_api() for d in items]})

    @r.get("/settings", responses=_doc(dict))
    async def get_settings() -> JSONResponse:
        return ok(ctx.settings.get().model_dump())

    @r.put("/settings", responses=_doc(dict))
    async def put_settings(body: SettingsPatch) -> JSONResponse:
        return ok(ctx.apply_settings(body))

    @r.get("/privacy", responses=_doc(M.PrivacyOut))
    async def privacy() -> JSONResponse:
        return ok(ctx.privacy())

    @r.post("/shutdown", status_code=202, responses=_doc(M.Accepted, 202))
    async def shutdown() -> JSONResponse:
        log(logger, logging.INFO, "shutdown requested via API")
        asyncio.get_running_loop().call_later(0.05, ctx.shutdown_event.set)
        return ok({"accepted": True}, 202)

    @r.post("/stt/model/download", status_code=202, responses=_doc(dict, 202),
            summary="Explicit user action: download the configured faster-whisper model")
    async def stt_download() -> JSONResponse:
        from ..stt.whisper import MODEL_CATALOG, FasterWhisperStt, faster_whisper_installed

        model = ctx.settings.get().stt.model
        if not faster_whisper_installed():
            raise ApiError("stt_model_unavailable", details={"reason": "faster_whisper_not_installed"})
        if model not in MODEL_CATALOG:
            raise ApiError("invalid_request", details={"reason": "unknown_model", "model": model})
        if ctx.model_manager.downloading:
            return ok(ctx.model_manager.describe(model), 202)

        async def run() -> None:
            try:
                await asyncio.to_thread(ctx.model_manager.download, model)
                engine = ctx.stt_worker.engine
                if isinstance(engine, FasterWhisperStt):
                    engine._state = "not_loaded"
                    engine.refresh_state()
                    await asyncio.to_thread(engine.ensure_loaded)
            except Exception as exc:  # noqa: BLE001
                log(logger, logging.ERROR, "model download failed", reason=type(exc).__name__)

        ctx.model_manager.downloading = model
        asyncio.get_running_loop().create_task(run())
        return ok(ctx.model_manager.describe(model), 202)

    # ------------------------------------------------------------------ meetings
    @r.get("/meetings", responses=_doc(M.MeetingList))
    async def list_meetings(limit: int = Query(50, ge=1, le=200), cursor: str | None = None) -> JSONResponse:
        return ok(ctx.meetings.list(limit, cursor))

    @r.post("/meetings", status_code=201, responses=_doc(M.MeetingOut, 201))
    async def create_meeting(body: M.CreateMeetingIn) -> JSONResponse:
        return ok(ctx.meetings.create(body.model_dump()), 201)

    @r.get("/meetings/{meeting_id}", responses=_doc(M.MeetingOut))
    async def get_meeting(meeting_id: str) -> JSONResponse:
        return ok(ctx.meetings.get(meeting_id))

    @r.patch("/meetings/{meeting_id}", responses=_doc(M.MeetingOut))
    async def patch_meeting(meeting_id: str, body: M.PatchMeetingIn) -> JSONResponse:
        settings = body.settings.model_dump(exclude_none=True) if body.settings else None
        return ok(ctx.meetings.patch(meeting_id, body.title, settings))

    @r.post("/meetings/{meeting_id}/consent", responses=_doc(M.MeetingOut))
    async def consent(meeting_id: str, body: M.ConsentEventIn) -> JSONResponse:
        return ok(await ctx.meetings.consent(meeting_id, body.scope, body.granted, body.policyVersion))

    @r.post("/meetings/{meeting_id}/start", responses=_doc(M.MeetingOut))
    async def start(meeting_id: str, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")
                    ) -> JSONResponse:
        return ok(await ctx.meetings.start(meeting_id, idempotency_key))

    @r.post("/meetings/{meeting_id}/stop", status_code=202, responses=_doc(M.MeetingOut, 202))
    async def stop(meeting_id: str) -> JSONResponse:
        return ok(await ctx.meetings.stop(meeting_id), 202)

    @r.post("/meetings/{meeting_id}/recover", status_code=202, responses=_doc(M.MeetingOut, 202))
    async def recover(meeting_id: str, body: M.RecoverIn) -> JSONResponse:
        return ok(await ctx.meetings.recover(meeting_id, body.action), 202)

    @r.delete("/meetings/{meeting_id}", responses=_doc(M.DeleteResult))
    async def delete_meeting(meeting_id: str) -> JSONResponse:
        return ok(await ctx.meetings.delete(meeting_id))

    # ------------------------------------------------------------------ transcript
    @r.get("/meetings/{meeting_id}/transcript", responses=_doc(M.SegmentList))
    async def transcript(meeting_id: str, afterMs: int | None = Query(None, ge=0),  # noqa: N803
                         limit: int = Query(200, ge=1, le=1000), includePartial: bool = False,  # noqa: N803
                         cursor: str | None = None) -> JSONResponse:
        return ok(ctx.meetings.transcript(meeting_id, afterMs, limit, includePartial, cursor))

    @r.patch("/meetings/{meeting_id}/transcript/{segment_id}", responses=_doc(M.SegmentOut))
    async def edit_segment(meeting_id: str, segment_id: str, body: M.EditSegmentIn) -> JSONResponse:
        fields = body.model_dump(exclude_unset=True)
        if not fields:
            raise ApiError("invalid_request", details={"reason": "empty_patch"})
        return ok(ctx.meetings.edit_segment(meeting_id, segment_id, fields))

    @r.get("/meetings/{meeting_id}/transcript/{segment_id}/history", responses=_doc(M.SegmentHistory))
    async def segment_history(meeting_id: str, segment_id: str) -> JSONResponse:
        return ok(ctx.meetings.segment_history(meeting_id, segment_id))

    # ------------------------------------------------------------------ summaries & notes
    @r.get("/meetings/{meeting_id}/summaries/preview", responses=_doc(M.SummaryPreview))
    async def summary_preview(meeting_id: str) -> JSONResponse:
        return ok(ctx.meetings.preview(meeting_id))

    @r.post("/meetings/{meeting_id}/summaries", status_code=202, responses=_doc(M.SummaryRequested, 202))
    async def request_summary(meeting_id: str) -> JSONResponse:
        return ok(await ctx.meetings.request_summary(meeting_id), 202)

    @r.get("/meetings/{meeting_id}/notes", responses=_doc(M.NotesOut))
    async def get_notes(meeting_id: str) -> JSONResponse:
        ctx.meetings.get_row(meeting_id)
        return ok(ctx.notes.get(meeting_id))

    @r.get("/meetings/{meeting_id}/notes/revisions", responses=_doc(M.NoteRevisionList))
    async def note_revisions(meeting_id: str) -> JSONResponse:
        ctx.meetings.get_row(meeting_id)
        return ok({"items": ctx.notes.list_revisions(meeting_id)})

    @r.put("/meetings/{meeting_id}/notes", responses=_doc(M.NoteRevisionOut))
    async def put_notes(meeting_id: str, body: M.PutNoteIn) -> JSONResponse:
        m = ctx.meetings.get_row(meeting_id)
        if m["state"] in ("deleting",):
            raise ApiError("invalid_state")
        return ok(ctx.notes.put_user(meeting_id, body.baseRevisionId, body.note))

    @r.post("/meetings/{meeting_id}/notes/conflict/resolve", responses=_doc(M.NoteRevisionOut))
    async def resolve_conflict(meeting_id: str, body: M.ResolveConflictIn) -> JSONResponse:
        ctx.meetings.get_row(meeting_id)
        return ok(ctx.notes.resolve_conflict(meeting_id, body.action, body.conflictRevisionId))

    @r.get("/meetings/{meeting_id}/export", responses=_doc(M.ExportOut))
    async def export(meeting_id: str, format: str = Query("markdown", pattern="^(markdown|json)$")) -> JSONResponse:  # noqa: A002
        return ok(await asyncio.to_thread(ctx.meetings.export, meeting_id, format))

    # ------------------------------------------------------------------ jobs
    @r.get("/meetings/{meeting_id}/jobs", responses=_doc(M.JobList))
    async def meeting_jobs(meeting_id: str) -> JSONResponse:
        return ok({"items": ctx.meetings.jobs(meeting_id)})

    def _job(job_id: str) -> dict[str, Any]:
        job = ctx.repo.get_job(job_id)
        if job is None:
            raise ApiError("not_found", details={"resource": "job"})
        m = ctx.repo.get_meeting(job["meeting_id"])
        if m is None or m["state"] == "deleted":
            raise ApiError("not_found", details={"resource": "job"})
        return job

    @r.get("/jobs/{job_id}", responses=_doc(M.JobOut))
    async def get_job(job_id: str) -> JSONResponse:
        return ok(job_to_api(_job(job_id)))

    @r.post("/jobs/{job_id}/retry", responses=_doc(M.JobOut))
    async def retry_job(job_id: str) -> JSONResponse:
        job = _job(job_id)
        allowed, details = ctx.meetings.summarization_allowed(job["meeting_id"])
        if not allowed:
            raise ApiError("consent_required", details=details)
        if job["status"] not in ("failed", "canceled", "retry_wait"):
            raise ApiError("invalid_state", details={"status": job["status"]})
        return ok(job_to_api(ctx.jobs.retry(job_id)))

    @r.post("/jobs/{job_id}/cancel", responses=_doc(M.JobOut))
    async def cancel_job(job_id: str) -> JSONResponse:
        job = _job(job_id)
        if job["status"] in ("succeeded", "failed", "canceled"):
            raise ApiError("invalid_state", details={"status": job["status"]})
        return ok(job_to_api(ctx.jobs.cancel(job_id)))

    # ------------------------------------------------------------------ events
    @r.websocket("/events")
    async def events(ws: WebSocket) -> None:
        origin = ws.headers.get("origin")
        has_proto, presented = ws_token(ws.headers.get("sec-websocket-protocol"))
        origin_ok = origin is None or origin in ctx.config.allowed_origins
        authorized = has_proto and token_matches(presented, ctx.token)
        if not host_allowed(ws.headers.get("host")) or not origin_ok or not authorized:
            log(logger, logging.WARNING, "websocket rejected", hasProtocol=has_proto, originAllowed=origin_ok)
            await ws.close(code=1008)
            return
        try:
            cursor = max(0, int(ws.query_params.get("cursor") or 0))
        except ValueError:
            await ws.close(code=1008)
            return
        sub = ctx.bus.subscribe()
        try:
            await ws.accept(subprotocol=WS_PROTOCOL)
            last = cursor
            for env in ctx.bus.replay(cursor):
                await ws.send_json(env)
                last = env["seq"]

            async def pump() -> None:
                nonlocal last
                while True:
                    env = await sub.queue.get()
                    if env is None:  # subscriber overflowed: client must reconnect with its cursor
                        await ws.close(code=1013)
                        return
                    if env["seq"] <= last:
                        continue
                    await ws.send_json(env)
                    last = env["seq"]

            async def drain_incoming() -> None:
                while True:
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        return

            tasks = [asyncio.create_task(pump()), asyncio.create_task(drain_incoming())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                exc = t.exception()
                if exc is not None and not isinstance(exc, WebSocketDisconnect):
                    raise exc
        except WebSocketDisconnect:
            pass
        finally:
            ctx.bus.unsubscribe(sub)
            if ws.application_state == WebSocketState.CONNECTED:
                try:
                    await ws.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    app.include_router(r)

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        from fastapi.openapi.utils import get_openapi

        schema = get_openapi(
            title=app.title, version=app.version, summary=app.summary, routes=app.routes,
            description=(
                "Normative contract: contracts/api-v1.md. Every response is an envelope "
                "`{requestId, data}` or `{requestId, error:{code,message,details}}`; `X-Request-Id` mirrors "
                "requestId. Mutating requests require `Content-Type: application/json`. The event stream is "
                "`WS /api/v1/events?cursor=<seq>` with subprotocols `sekretaer.v1, bearer.<token>` "
                "(envelope: contracts/events/event-1.0.schema.json)."
            ),
        )
        schema.setdefault("components", {}).setdefault("securitySchemes", {})["bearerAuth"] = {
            "type": "http", "scheme": "bearer", "description": "Random token from the sidecar ready line."}
        schema["security"] = [{"bearerAuth": []}]
        schema["servers"] = [{"url": "http://127.0.0.1:{port}", "variables": {"port": {"default": "0"}}}]
        schema["info"]["x-websocket"] = {
            "path": f"{PREFIX}/events", "query": {"cursor": "last seen seq (integer, optional)"},
            "subprotocols": [WS_PROTOCOL, "bearer.<token>"], "echoedSubprotocol": WS_PROTOCOL,
            "eventSchema": "contracts/events/event-1.0.schema.json",
            "ephemeralEvents": ["transcript.partial", "audio.level"],
        }
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]
    app.add_middleware(SecurityMiddleware, token_getter=lambda: ctx.token, allowed_origins=ctx.config.allowed_origins)
    return app
