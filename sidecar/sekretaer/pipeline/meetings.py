"""Meeting lifecycle and API-facing operations (spec §2, §8, appendix A state machine).

preparing -> recording -> finalizing -> completed; input problems keep ``recording`` with ``degraded``
markers; deletion goes deleting -> deleted. Meetings found in ``recording``/``finalizing`` at startup
are never resumed automatically; they get the ``interrupted`` marker and need ``/recover``.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import CONSENT_POLICY_VERSION
from ..audio.base import AudioSource, AudioSourceError, PermissionDenied
from ..errors import ApiError
from ..jobs.queue import job_to_api
from ..logsetup import log
from ..privacy.deletion import delete_meeting_data
from ..privacy.export import build_json, build_markdown, slug, write_export
from ..util import dumps, ms_between, now_dt, now_iso, parse_iso, sha256_hex
from ..vad.models import create_vad
from .breakpoints import BreakpointPolicy
from .session import MeetingSession, SessionTuning

if TYPE_CHECKING:  # pragma: no cover
    from ..app_context import AppContext

logger = logging.getLogger("sekretaer.meetings")

FINAL_STATES = ("completed", "error")


def segment_to_api(seg: dict[str, Any], kinds: dict[str, str]) -> dict[str, Any]:
    from ..util import loads

    meta = loads(seg.get("stt_metadata_json"), {}) or {}
    return {
        "id": seg["id"],
        "meetingId": seg["meeting_id"],
        "sourceId": seg["source_id"],
        "source": kinds.get(seg["source_id"], "microphone"),
        "startMs": int(seg["start_ms"]),
        "endMs": int(seg["end_ms"]),
        "text": seg["text"],
        "language": seg["language"],
        "isFinal": bool(seg["is_final"]),
        "revision": int(seg["revision"]),
        "speakerLabel": seg["speaker_label"],
        "stt": {"avgLogprob": meta.get("avgLogprob"), "noSpeechProb": meta.get("noSpeechProb")},
    }


def _encode_cursor(*parts: Any) -> str:
    return base64.urlsafe_b64encode(dumps(list(parts)).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> list[Any] | None:
    if not cursor:
        return None
    try:
        from ..util import loads

        pad = "=" * (-len(cursor) % 4)
        value = loads(base64.urlsafe_b64decode(cursor + pad).decode())
        if not isinstance(value, list):
            raise ValueError
        return value
    except Exception as exc:  # noqa: BLE001
        raise ApiError("invalid_request", details={"field": "cursor"}) from exc


class MeetingService:
    def __init__(self, ctx: AppContext, policy: BreakpointPolicy | None = None,
                 tuning: SessionTuning | None = None) -> None:
        self.ctx = ctx
        self.config = ctx.config
        self.repo = ctx.repo
        self.bus = ctx.bus
        self.stt_worker = ctx.stt_worker
        self.cipher = ctx.cipher
        self.policy = policy
        self.tuning = tuning
        self.sessions: dict[str, MeetingSession] = {}
        self._runtime_degraded: dict[str, set[str]] = {}
        self._start_lock = asyncio.Lock()
        self._finalizers: dict[str, asyncio.Task[None]] = {}

    # ------------------------------------------------------------------ representation
    def get_row(self, meeting_id: str, *, allow_deleted: bool = False) -> dict[str, Any]:
        m = self.repo.get_meeting(meeting_id)
        if m is None or (m["state"] == "deleted" and not allow_deleted):
            raise ApiError("not_found", details={"resource": "meeting"})
        return m

    def to_api(self, m: dict[str, Any]) -> dict[str, Any]:
        mid = m["id"]
        consent = self.repo.consent_state(mid)
        settings = m["settings"] or {}
        started = m["started_at"]
        elapsed = 0
        if started:
            end = parse_iso(m["ended_at"]) if m["ended_at"] else None
            elapsed = ms_between(started, end)
        degraded = sorted(set(m["degraded"]) | self._runtime_degraded.get(mid, set()))
        return {
            "id": mid,
            "title": m["title"],
            "language": m["language"],
            "state": m["state"],
            "degraded": degraded,
            "startedAt": started,
            "endedAt": m["ended_at"],
            "timezone": m["timezone"],
            "createdAt": m["created_at"],
            "updatedAt": m["updated_at"],
            "elapsedMs": elapsed,
            "sources": [
                {"id": s["id"], "kind": s["kind"], "deviceKey": s["device_key"], "sampleRate": s["sample_rate"],
                 "channels": s["channels"], "permissionState": s["permission_state"], "offsetMs": s["offset_ms"]}
                for s in self.repo.list_sources(mid)
            ],
            "consent": {"recording": consent["recording"], "externalProcessing": consent["external_processing"]},
            "settings": {
                "summarizationEnabled": bool(settings.get("summarizationEnabled", False)),
                "retainAudio": bool(settings.get("retainAudio", False)),
                "audioRetentionDays": int(settings.get("audioRetentionDays", 7)),
            },
            "processing": {
                "sttBacklogMs": self.stt_worker.backlog_ms(mid),
                "pendingJobs": self.repo.count_active_jobs(mid),
                "lastError": m["last_error"],
            },
        }

    def get(self, meeting_id: str) -> dict[str, Any]:
        return self.to_api(self.get_row(meeting_id))

    def list(self, limit: int, cursor: str | None) -> dict[str, Any]:
        cur = _decode_cursor(cursor)
        rows = self.repo.list_meetings(limit + 1, (str(cur[0]), str(cur[1])) if cur else None)
        items = [self.to_api(m) for m in rows[:limit]]
        next_cursor = _encode_cursor(rows[limit - 1]["created_at"], rows[limit - 1]["id"]) if len(rows) > limit \
            else None
        return {"items": items, "nextCursor": next_cursor}

    def publish_state(self, meeting_id: str) -> None:
        m = self.repo.get_meeting(meeting_id)
        if m is None:
            return
        degraded = sorted(set(m["degraded"]) | self._runtime_degraded.get(meeting_id, set()))
        self.bus.publish("meeting.state", meeting_id, {"state": m["state"], "degraded": degraded,
                                                       "startedAt": m["started_at"], "endedAt": m["ended_at"]})

    def add_degraded(self, meeting_id: str, code: str, *, persist: bool = True) -> None:
        if persist:
            try:
                m = self.repo.get_meeting(meeting_id)
                if m is not None and code not in m["degraded"]:
                    self.repo.update_meeting(meeting_id, degraded=[*m["degraded"], code])
            except Exception:  # noqa: BLE001 - fall back to runtime marker
                self._runtime_degraded.setdefault(meeting_id, set()).add(code)
        else:
            self._runtime_degraded.setdefault(meeting_id, set()).add(code)
        self.publish_state(meeting_id)

    def remove_degraded(self, meeting_id: str, code: str) -> None:
        self._runtime_degraded.get(meeting_id, set()).discard(code)
        m = self.repo.get_meeting(meeting_id)
        if m is not None and code in m["degraded"]:
            self.repo.update_meeting(meeting_id, degraded=[d for d in m["degraded"] if d != code])
        self.publish_state(meeting_id)

    def elapsed_ms(self, meeting_id: str) -> int:
        m = self.repo.get_meeting(meeting_id)
        return ms_between(m["started_at"]) if m and m["started_at"] else 0

    def retain_audio(self, meeting_id: str) -> bool:
        m = self.repo.get_meeting(meeting_id)
        return bool(m and (m["settings"] or {}).get("retainAudio"))

    def publish_privacy(self, meeting_id: str) -> None:
        m = self.repo.get_meeting(meeting_id)
        if m is None:
            return
        api = self.to_api(m)
        self.bus.publish("privacy.state", meeting_id, {"consent": api["consent"], "settings": api["settings"]})

    # ------------------------------------------------------------------ create / patch / consent
    def _check_retention_possible(self) -> None:
        ok, reason = self.cipher.available()
        if not ok:
            raise ApiError(
                "invalid_request",
                "音声を暗号化して保存するための機能（cryptography / OS の資格情報ストア）が利用できないため、"
                "音声の保持を有効にできません。",
                details={"reason": "secure_storage_unavailable", "detail": reason},
            )

    def create(self, body: dict[str, Any]) -> dict[str, Any]:
        settings_in = body.get("settings") or {}
        privacy = self.ctx.settings.get().privacy
        settings = {
            "summarizationEnabled": bool(settings_in.get("summarizationEnabled", False)),
            "retainAudio": bool(settings_in.get("retainAudio", privacy.defaultRetainAudio)),
            "audioRetentionDays": int(settings_in.get("audioRetentionDays", privacy.defaultAudioRetentionDays)),
        }
        if settings["retainAudio"]:
            self._check_retention_possible()
        with self.repo.db.tx():
            mid = self.repo.insert_meeting(title=body["title"], language=body.get("language", "auto"),
                                           timezone=body.get("timezone") or _tz(), settings=settings)
            for src in body.get("sources") or []:
                self.repo.insert_source(mid, src["kind"], src["deviceKey"])
            consent = body.get("consent") or {}
            policy = body.get("policyVersion") or CONSENT_POLICY_VERSION
            if consent.get("recording"):
                self.repo.insert_consent(mid, "recording", True, policy, "granted")
            if consent.get("externalProcessing"):
                self.repo.insert_consent(mid, "external_processing", True, policy, "granted")
        self.publish_state(mid)
        return self.get(mid)

    def patch(self, meeting_id: str, title: str | None, settings: dict[str, Any] | None) -> dict[str, Any]:
        m = self.get_row(meeting_id)
        if m["state"] in ("deleting", "deleted"):
            raise ApiError("invalid_state")
        fields: dict[str, Any] = {}
        if title is not None:
            fields["title"] = title
        privacy_changed = False
        if settings:
            new = dict(m["settings"] or {})
            if settings.get("retainAudio") and not new.get("retainAudio"):
                self._check_retention_possible()
            new.update({k: v for k, v in settings.items() if v is not None})
            fields["settings"] = new
            privacy_changed = True
            if not new.get("summarizationEnabled"):
                self.ctx.jobs.cancel_meeting(meeting_id)
        self.repo.update_meeting(meeting_id, **fields)
        if privacy_changed:
            self.publish_privacy(meeting_id)
        return self.get(meeting_id)

    async def consent(self, meeting_id: str, scope: str, granted: bool, policy_version: str | None) -> dict[str, Any]:
        m = self.get_row(meeting_id)
        if m["state"] in ("deleting", "deleted"):
            raise ApiError("invalid_state")
        prev = self.repo.consent_state(meeting_id)[scope]
        event_type = "granted" if granted else ("withdrawn" if prev == "granted" else "denied")
        self.repo.insert_consent(meeting_id, scope, granted, policy_version or CONSENT_POLICY_VERSION, event_type)
        if not granted and scope == "external_processing":
            self.ctx.jobs.cancel_meeting(meeting_id)
        if not granted and scope == "recording" and m["state"] == "recording":
            # Withdrawal stops new capture immediately (appendix A 同意).
            await self.stop(meeting_id)
        self.publish_privacy(meeting_id)
        return self.get(meeting_id)

    # ------------------------------------------------------------------ start (AC-01)
    async def start(self, meeting_id: str, idem_key: str | None) -> dict[str, Any]:
        if not idem_key or len(idem_key) > 200:
            raise ApiError("invalid_request", "Idempotency-Key ヘッダーが必要です。",
                           details={"header": "Idempotency-Key"})
        scope = f"start:{meeting_id}"
        cached = self.repo.get_idempotent(scope, idem_key)
        if cached is not None:
            return cached["body"]
        async with self._start_lock:
            cached = self.repo.get_idempotent(scope, idem_key)
            if cached is not None:
                return cached["body"]
            m = self.get_row(meeting_id)
            if m["state"] != "preparing":
                raise ApiError("invalid_state", details={"state": m["state"]})
            consent = self.repo.consent_state(meeting_id)
            if consent["recording"] != "granted":
                raise ApiError("consent_required",
                               "録音の同意が記録されていません。参加者への通知と同意を確認してください。",
                               details={"scope": "recording", "state": consent["recording"],
                                        "settingsPath": "meeting.privacy.consent"})
            busy = [mid for mid, s in self.sessions.items() if not s.stopping]
            if busy:
                raise ApiError("invalid_state", "別の会議が録音中です。",
                               details={"reason": "another_meeting_recording", "meetingId": busy[0]})
            sources = self.repo.list_sources(meeting_id)
            if not sources:
                raise ApiError("source_unavailable", "音声入力ソースが選択されていません。",
                               details={"reason": "no_sources", "sources": []})
            opened, report = await self._open_sources(sources)
            unavailable = [r for r in report if r["permissionState"] == "unavailable"]
            if not opened:
                raise ApiError("source_unavailable", details={"sources": report,
                                                               "settingsPath": "meeting.sources"})
            self.repo.update_meeting(meeting_id, state="recording", started_at=now_iso(), ended_at=None)
            m = self.get_row(meeting_id)
            vad_sensitivity = self.ctx.settings.get().vad.sensitivity

            def vad_factory() -> tuple[Any, float]:
                model, info = create_vad(vad_sensitivity, prefer_silero=self.ctx.prefer_silero)
                self.ctx.vad_info = info
                return model, model.threshold(vad_sensitivity)

            session = MeetingSession(service=self, meeting=m, sources=opened, vad_factory=vad_factory,
                                     policy=self.policy, tuning=self.tuning)
            self.sessions[meeting_id] = session
            await session.start()
            if unavailable:
                self.add_degraded(meeting_id, "source_unavailable")
                session.warning("source_unavailable", {"sources": unavailable})
            self.publish_state(meeting_id)
            body = self.get(meeting_id)
            self.repo.put_idempotent(scope, idem_key, 200, body, meeting_id)
            log(logger, logging.INFO, "meeting started", meetingId=meeting_id, sources=len(opened))
            return body

    async def _open_sources(
        self, sources: list[dict[str, Any]]
    ) -> tuple[list[tuple[dict[str, Any], AudioSource]], list[dict[str, Any]]]:
        backend = self.ctx.backend
        report: list[dict[str, Any]] = []
        states: dict[str, str] = {}
        for s in sources:
            state = backend.probe(s["kind"], s["device_key"])
            states[s["id"]] = state
        denied = [s for s in sources if states[s["id"]] == "denied"]
        for s in sources:
            self.repo.update_source(s["id"], permission_state=states[s["id"]])
        if denied:
            raise ApiError("permission_denied", details={
                "sources": [{"sourceId": s["id"], "kind": s["kind"], "deviceKey": s["device_key"]} for s in denied],
                "settingsPath": "os.privacy.microphone"})
        opened: list[tuple[dict[str, Any], AudioSource]] = []
        try:
            for s in sources:
                if states[s["id"]] == "unavailable":
                    report.append({"sourceId": s["id"], "kind": s["kind"], "permissionState": "unavailable"})
                    continue
                try:
                    adapter = backend.open(s["kind"], s["device_key"])
                    await adapter.start()
                except PermissionDenied as exc:
                    self.repo.update_source(s["id"], permission_state="denied")
                    raise ApiError("permission_denied", details={
                        "sources": [{"sourceId": s["id"], "kind": s["kind"], "deviceKey": s["device_key"]}],
                        "settingsPath": "os.privacy.microphone"}) from exc
                except AudioSourceError:
                    self.repo.update_source(s["id"], permission_state="unavailable")
                    report.append({"sourceId": s["id"], "kind": s["kind"], "permissionState": "unavailable"})
                    continue
                if states[s["id"]] == "unknown":
                    self.repo.update_source(s["id"], permission_state="granted")
                opened.append((self.repo.get_source(s["id"]) or s, adapter))
                report.append({"sourceId": s["id"], "kind": s["kind"], "permissionState": "granted"})
        except ApiError:
            for _row, adapter in opened:
                try:
                    await adapter.stop()
                except Exception:  # noqa: BLE001, S110
                    pass
            raise
        return opened, report

    # ------------------------------------------------------------------ stop / recover / finalize
    async def stop(self, meeting_id: str) -> dict[str, Any]:
        m = self.get_row(meeting_id)
        if m["state"] == "finalizing" and meeting_id in self._finalizers:
            return self.to_api(m)
        if m["state"] not in ("recording", "finalizing"):
            raise ApiError("invalid_state", details={"state": m["state"]})
        self._begin_finalize(meeting_id)
        return self.get(meeting_id)

    async def recover(self, meeting_id: str, action: str) -> dict[str, Any]:
        m = self.get_row(meeting_id)
        if action != "finalize":
            raise ApiError("invalid_request", details={"field": "action"})
        if m["state"] not in ("recording", "finalizing") or meeting_id in self.sessions \
                or meeting_id in self._finalizers:
            raise ApiError("invalid_state", details={"state": m["state"]})
        self._begin_finalize(meeting_id)
        return self.get(meeting_id)

    def _begin_finalize(self, meeting_id: str) -> None:
        m = self.repo.get_meeting(meeting_id)
        assert m is not None
        fields: dict[str, Any] = {"state": "finalizing"}
        if not m["ended_at"]:
            fields["ended_at"] = now_iso()
        self.repo.update_meeting(meeting_id, **fields)
        self.publish_state(meeting_id)
        self._finalizers[meeting_id] = asyncio.create_task(self._finalize(meeting_id), name=f"finalize-{meeting_id}")

    async def _finalize(self, meeting_id: str) -> None:
        try:
            session = self.sessions.get(meeting_id)
            if session is not None:
                await session.stop_and_drain()
                self.sessions.pop(meeting_id, None)
            self.repo.delete_partials(meeting_id)
            self.create_auto_job(meeting_id, "final", None)
            m = self.repo.get_meeting(meeting_id)
            if m is None or m["state"] != "finalizing":
                return
            degraded = [d for d in m["degraded"] if d not in ("stt_queue_backlog", "interrupted")]
            self.repo.update_meeting(meeting_id, state="completed", degraded=degraded)
            self._runtime_degraded.pop(meeting_id, None)
            self.publish_state(meeting_id)
            log(logger, logging.INFO, "meeting completed", meetingId=meeting_id)
        except Exception:  # noqa: BLE001
            logger.exception("finalize failed", extra={"fields": {"meetingId": meeting_id}})
            try:
                self.repo.update_meeting(meeting_id, state="error", last_error="db_write_failed")
                self.publish_state(meeting_id)
            except Exception:  # noqa: BLE001, S110
                pass
        finally:
            self._finalizers.pop(meeting_id, None)

    async def wait_finalized(self, meeting_id: str) -> None:
        task = self._finalizers.get(meeting_id)
        if task is not None:
            await asyncio.shield(task)

    def recover_on_startup(self) -> list[str]:
        ids = []
        for m in self.repo.meetings_in_states(("recording", "finalizing")):
            if "interrupted" not in m["degraded"]:
                self.repo.update_meeting(m["id"], degraded=[*m["degraded"], "interrupted"])
            ids.append(m["id"])
            log(logger, logging.WARNING, "meeting interrupted by previous shutdown", meetingId=m["id"])
        return ids

    async def shutdown(self) -> None:
        for session in list(self.sessions.values()):
            await session.abort()
        self.sessions.clear()
        for task in list(self._finalizers.values()):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110
                pass

    # ------------------------------------------------------------------ summaries
    def summarization_allowed(self, meeting_id: str) -> tuple[bool, dict[str, Any]]:
        m = self.repo.get_meeting(meeting_id)
        if m is None:
            return False, {"reason": "not_found"}
        consent = self.repo.consent_state(meeting_id)["external_processing"]
        enabled = bool((m["settings"] or {}).get("summarizationEnabled"))
        global_enabled = self.ctx.settings.get().summarizer.enabled
        if consent != "granted" or not enabled or not global_enabled:
            return False, {"scope": "external_processing", "state": consent, "summarizationEnabled": enabled,
                           "globalEnabled": global_enabled, "settingsPath": "meeting.privacy.externalProcessing"}
        return True, {}

    def _create_job(self, meeting_id: str, trigger: str, range_end_ms: int | None) -> tuple[dict, bool] | None:
        cursor = self.repo.claimed_cursor_ms(meeting_id)
        end = range_end_ms if range_end_ms is not None else self.repo.last_final_segment_end(meeting_id)
        segs = self.repo.final_segments_in_range(meeting_id, cursor, end)
        if not segs:
            return None
        base = self.repo.current_note(meeting_id)
        input_hash = sha256_hex(dumps({
            "meetingId": meeting_id, "segments": [[s["id"], s["revision"]] for s in segs],
            "base": base["id"] if base else None,
        }))
        job, dedup = self.repo.insert_job(
            meeting_id=meeting_id, trigger=trigger, input_hash=input_hash, range_start_ms=cursor,
            range_end_ms=max(end, int(segs[-1]["end_ms"])), segment_ids=[s["id"] for s in segs],
            base_revision_id=base["id"] if base else None, max_attempts=3,
        )
        if dedup:
            if job["status"] in ("failed", "canceled"):
                self.repo.update_job(job["id"], status="queued", attempt=0, error_code=None, next_retry_at=None,
                                     finished_at=None)
                job = self.repo.get_job(job["id"]) or job
        else:
            self.bus.publish("summary.queued", meeting_id, {"jobId": job["id"], "trigger": trigger,
                                                            "rangeStartMs": cursor, "rangeEndMs": job["range_end_ms"]})
            log(logger, logging.INFO, "summary job queued", meetingId=meeting_id, jobId=job["id"], trigger=trigger,
                segments=len(segs))
        self.ctx.jobs.wake()
        return job, dedup

    def create_auto_job(self, meeting_id: str, trigger: str, range_end_ms: int | None) -> dict[str, Any] | None:
        allowed, _ = self.summarization_allowed(meeting_id)
        if not allowed:
            return None
        res = self._create_job(meeting_id, trigger, range_end_ms)
        return res[0] if res else None

    async def request_summary(self, meeting_id: str) -> dict[str, Any]:
        m = self.get_row(meeting_id)
        if m["state"] in ("deleting", "deleted"):
            raise ApiError("invalid_state")
        allowed, details = self.summarization_allowed(meeting_id)
        if not allowed:
            raise ApiError("consent_required",
                           "外部処理（Claude CLI への送信）の同意または要約設定が有効ではありません。",
                           details=details)
        session = self.sessions.get(meeting_id)
        now_ms = await session.prepare_manual() if session is not None else None
        res = self._create_job(meeting_id, "manual", None)
        if res is None:
            latest = self.repo.latest_job(meeting_id)
            if latest is None:
                raise ApiError("invalid_state", "要約対象となる確定済みの発話がまだありません。",
                               details={"reason": "nothing_to_summarize"})
            return {"jobId": latest["id"], "deduplicated": True}
        job, dedup = res
        if session is not None and now_ms is not None:
            session.detector.on_job_created(int(job["range_end_ms"]), now_ms)
        return {"jobId": job["id"], "deduplicated": dedup}

    def preview(self, meeting_id: str) -> dict[str, Any]:
        self.get_row(meeting_id)
        return self.ctx.summarizer.preview(meeting_id)

    def jobs(self, meeting_id: str) -> list[dict[str, Any]]:
        self.get_row(meeting_id)
        return [job_to_api(j) for j in self.repo.list_jobs(meeting_id)]

    # ------------------------------------------------------------------ transcript
    def source_kinds(self, meeting_id: str) -> dict[str, str]:
        return {s["id"]: s["kind"] for s in self.repo.list_sources(meeting_id)}

    def transcript(self, meeting_id: str, after_ms: int | None, limit: int, include_partial: bool,
                   cursor: str | None) -> dict[str, Any]:
        self.get_row(meeting_id)
        cur = _decode_cursor(cursor)
        rows = self.repo.list_segments(meeting_id, after_ms=after_ms, limit=limit + 1,
                                       include_partial=include_partial,
                                       cursor=(int(cur[0]), str(cur[1])) if cur else None)
        kinds = self.source_kinds(meeting_id)
        items = [segment_to_api(r, kinds) for r in rows[:limit]]
        nxt = _encode_cursor(rows[limit - 1]["start_ms"], rows[limit - 1]["id"]) if len(rows) > limit else None
        return {"items": items, "nextCursor": nxt}

    def edit_segment(self, meeting_id: str, segment_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        m = self.get_row(meeting_id)
        if m["state"] in ("deleting",):
            raise ApiError("invalid_state")
        seg = self.repo.edit_segment(meeting_id, segment_id, text=fields.get("text"),
                                     speaker_label=fields.get("speakerLabel"),
                                     set_speaker="speakerLabel" in fields)
        if seg is None:
            raise ApiError("not_found", details={"resource": "segment"})
        api = segment_to_api(seg, self.source_kinds(meeting_id))
        self.bus.publish("transcript.final", meeting_id, {
            "segmentId": api["id"], "sourceId": api["sourceId"], "source": api["source"], "startMs": api["startMs"],
            "endMs": api["endMs"], "text": api["text"], "isFinal": True, "revision": api["revision"],
            "speakerLabel": api["speakerLabel"], "origin": "user",
        })
        return api

    def segment_history(self, meeting_id: str, segment_id: str) -> dict[str, Any]:
        self.get_row(meeting_id)
        if self.repo.get_segment(meeting_id, segment_id) is None:
            raise ApiError("not_found", details={"resource": "segment"})
        return {"items": [
            {"revision": h["revision"], "text": h["text"], "speakerLabel": h["speaker_label"],
             "editedAt": h["edited_at"], "origin": h["origin"]}
            for h in self.repo.segment_history(segment_id)
        ]}

    # ------------------------------------------------------------------ delete / export
    async def delete(self, meeting_id: str) -> dict[str, Any]:
        m = self.get_row(meeting_id)
        self.repo.update_meeting(meeting_id, state="deleting")
        self.publish_state(meeting_id)
        session = self.sessions.pop(meeting_id, None)
        if session is not None:
            await session.abort()
        fin = self._finalizers.pop(meeting_id, None)
        if fin is not None:
            fin.cancel()
            try:
                await fin
            except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110
                pass
        self.stt_worker.cancel_meeting(meeting_id)
        self.ctx.jobs.cancel_meeting(meeting_id)  # kills a running claude process
        result = await asyncio.to_thread(
            delete_meeting_data, config=self.config, repo=self.repo, cipher=self.cipher, meeting_id=meeting_id
        )
        self._runtime_degraded.pop(meeting_id, None)
        if result["status"] == "deleted":
            self.bus.publish("meeting.state", meeting_id, {"state": "deleted", "degraded": []})
        else:
            self.publish_state(meeting_id)
        del m
        return result

    def export(self, meeting_id: str, fmt: str) -> dict[str, Any]:
        m = self.get_row(meeting_id)
        if m["state"] in ("deleting",):
            raise ApiError("invalid_state")
        api = self.to_api(m)
        current = self.repo.current_note(meeting_id)
        note = current["payload"] if current else None
        kinds = self.source_kinds(meeting_id)
        segments = [segment_to_api(s, kinds) for s in self.repo.list_segments(meeting_id)]
        date = (m["started_at"] or m["created_at"])[:10]
        base = f"sekretaer-{date}-{slug(m['title'])}"
        if fmt == "markdown":
            content = build_markdown(api, note, segments)
            filename, ctype = f"{base}.md", "text/markdown; charset=utf-8"
        else:
            from ..pipeline.notes import revision_meta_to_api

            revisions = [revision_meta_to_api(r) for r in self.repo.list_note_revisions(meeting_id)]
            content = build_json(api, note, segments, revisions)
            filename, ctype = f"{base}.json", "application/json"
        write_export(self.config.meeting_exports_dir(meeting_id), filename, content)
        return {"filename": filename, "contentType": ctype, "content": content}

    # ------------------------------------------------------------------ retention
    def sweep_retention(self) -> int:
        """Purge archived audio of finished meetings whose retention period has passed."""
        purged = 0
        now = now_dt()
        for m in self.repo.meetings_in_states(("completed", "error")):
            settings = m["settings"] or {}
            ended = parse_iso(m["ended_at"])
            if ended is None:
                continue
            days = int(settings.get("audioRetentionDays", 7))
            if now - ended < timedelta(days=days):
                continue
            meeting_purged = 0
            for chunk in self.repo.list_chunks(m["id"]):
                path = chunk.get("storage_path")
                if chunk["status"] != "stored" or not path:
                    continue
                try:
                    Path(path).unlink(missing_ok=True)
                    self.repo.db.execute(
                        "UPDATE audio_chunks SET status='purged', storage_path=NULL, sha256=NULL WHERE id=?",
                        (chunk["id"],))
                    meeting_purged += 1
                except OSError:
                    continue
            if meeting_purged:
                purged += meeting_purged
                self.cipher.destroy_key(m["id"])
        return purged


def _tz() -> str:
    from ..util import local_timezone_name

    return local_timezone_name()
