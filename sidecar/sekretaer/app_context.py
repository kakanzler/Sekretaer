"""Composition root: builds storage, event bus, audio backends, STT, VAD, summarizer, job queue and the
meeting service. Tests inject fakes (synthetic backend, scripted STT, fake CLI, in-memory secrets)."""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any

from . import API_VERSION, __version__
from .audio.base import AudioBackend, CompositeBackend
from .config import AppConfig
from .jobs.queue import JobQueue
from .logsetup import log
from .pipeline.breakpoints import BreakpointPolicy
from .pipeline.events import EventBus
from .pipeline.meetings import MeetingService
from .pipeline.notes import NoteService
from .pipeline.session import SessionTuning
from .pipeline.stt_worker import SttWorker
from .privacy.crypto import AudioCipher, KeyringSecretStore, SecretStore
from .settings import SettingsPatch, SettingsStore
from .storage.db import Database
from .storage.repo import Repo
from .stt.base import SttEngine, UnavailableStt
from .stt.whisper import FasterWhisperStt, ModelManager, faster_whisper_installed
from .summarization.cli import ClaudeCli
from .summarization.service import Summarizer
from .vad.models import create_vad

logger = logging.getLogger("sekretaer.app")
RETENTION_SWEEP_S = 3600


class AppContext:
    def __init__(
        self, config: AppConfig, *, backend: AudioBackend | None = None, stt_engine: SttEngine | None = None,
        secret_store: SecretStore | None = None, cli_path: str | None = None, cli_prefix_args: list[str] | None = None,
        prefer_silero: bool = True, policy: BreakpointPolicy | None = None, tuning: SessionTuning | None = None,
        token: str | None = None,
    ) -> None:
        self.config = config
        config.ensure_dirs()
        self.token = token or secrets.token_urlsafe(32)
        self.db = Database(config.db_path)
        self.repo = Repo(self.db)
        self.bus = EventBus(self.db)
        self.settings = SettingsStore(self.repo)
        self.prefer_silero = prefer_silero
        self.audio_notes: dict[str, str] = {}
        if backend is None:
            from .audio.devices import live_backends

            backends, self.audio_notes = live_backends()
            if config.synthetic_devices:
                from .audio.synthetic import demo_backend

                backends.insert(0, demo_backend())
            backend = CompositeBackend(backends)
        self.backend = backend
        self.model_manager = ModelManager(config.models_dir)
        s = self.settings.get()
        if stt_engine is None:
            if faster_whisper_installed():
                stt_engine = FasterWhisperStt(self.model_manager, s.stt.model, s.stt.device, s.stt.computeType)
            else:
                stt_engine = UnavailableStt()
                stt_engine.model = s.stt.model
                stt_engine.device = s.stt.device
                stt_engine.compute_type = s.stt.computeType
        self.stt_worker = SttWorker(stt_engine)
        self.cipher = AudioCipher(secret_store or KeyringSecretStore())
        self._default_cli_path = cli_path
        self.cli = ClaudeCli(
            workdir=config.tmp_dir / "cli-work", cli_path=s.summarizer.cliPath or cli_path,
            prefix_args=cli_prefix_args, timeout_s=float(s.summarizer.timeoutSec), model=s.summarizer.model,
        )
        self.summarizer = Summarizer(self.repo, self.cli, s.summarizer.maxInputChars)
        self.notes = NoteService(self.repo, self.bus)
        self.jobs = JobQueue(self.repo, self.bus, self.summarizer, apply_note=self.notes.apply_ai,
                             on_meeting_changed=self._on_jobs_changed)
        self.meetings = MeetingService(self, policy=policy, tuning=tuning)
        _vad, self.vad_info = create_vad(s.vad.sensitivity, prefer_silero=prefer_silero)
        self.status = "starting"
        self.shutdown_event = asyncio.Event()
        self._bg: list[asyncio.Task[Any]] = []
        self._cli_detected = False

    # ------------------------------------------------------------------ lifecycle
    async def startup(self) -> None:
        self.bus.loop = asyncio.get_running_loop()
        requeued = self.jobs.recover_on_startup()
        interrupted = self.meetings.recover_on_startup()
        self.stt_worker.start()
        self.jobs.start()
        self._bg.append(asyncio.create_task(self._detect_cli(), name="cli-detect"))
        self._bg.append(asyncio.create_task(self._preload_stt(), name="stt-preload"))
        self._bg.append(asyncio.create_task(self._retention_loop(), name="retention"))
        log(logger, logging.INFO, "sidecar started", version=__version__, requeuedJobs=requeued,
            interruptedMeetings=len(interrupted), sttState=self.stt_worker.engine.state,
            vad=self.vad_info.get("engine"))

    async def _detect_cli(self) -> None:
        try:
            await self.cli.detect()
        finally:
            self._cli_detected = True
            self.status = "ok"

    async def _preload_stt(self) -> None:
        engine = self.stt_worker.engine
        if isinstance(engine, FasterWhisperStt) and self.model_manager.is_downloaded(engine.model):
            try:
                await asyncio.to_thread(engine.ensure_loaded)
            except Exception as exc:  # noqa: BLE001 - state is reported via /health
                log(logger, logging.WARNING, "stt preload failed", reason=type(exc).__name__)

    async def _retention_loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.meetings.sweep_retention)
            except Exception:  # noqa: BLE001
                logger.exception("retention sweep failed")
            await asyncio.sleep(RETENTION_SWEEP_S)

    async def shutdown(self) -> None:
        for t in self._bg:
            t.cancel()
        await self.meetings.shutdown()
        await self.jobs.stop()
        await self.stt_worker.stop()
        for t in self._bg:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110
                pass
        self.db.close()
        log(logger, logging.INFO, "sidecar stopped")

    def _on_jobs_changed(self, meeting_id: str) -> None:
        pass

    # ------------------------------------------------------------------ settings
    def apply_settings(self, patch: SettingsPatch) -> dict[str, Any]:
        from pydantic import ValidationError

        from .errors import ApiError

        old = self.settings.get().model_copy(deep=True)
        try:
            new = self.settings.update(patch)
        except ValidationError as exc:
            errors = [{"loc": [str(p) for p in e.get("loc", [])], "type": e.get("type")} for e in exc.errors()]
            raise ApiError("invalid_request", details={"errors": errors[:20]}) from None
        self.cli.configure(cli_path=new.summarizer.cliPath or self._default_cli_path,
                           timeout_s=float(new.summarizer.timeoutSec), model=new.summarizer.model)
        self.summarizer.max_input_chars = new.summarizer.maxInputChars
        if new.summarizer.cliPath != old.summarizer.cliPath:
            self._bg.append(asyncio.create_task(self.cli.detect()))
        engine = self.stt_worker.engine
        if isinstance(engine, UnavailableStt):
            engine.model, engine.device, engine.compute_type = new.stt.model, new.stt.device, new.stt.computeType
        if isinstance(engine, FasterWhisperStt) and (
            new.stt.model != old.stt.model or new.stt.device != old.stt.device
            or new.stt.computeType != old.stt.computeType
        ):
            if not self.meetings.sessions:
                self.stt_worker.engine = FasterWhisperStt(self.model_manager, new.stt.model, new.stt.device,
                                                          new.stt.computeType)
                self._bg.append(asyncio.create_task(self._preload_stt()))
        _vad, self.vad_info = create_vad(new.vad.sensitivity, prefer_silero=self.prefer_silero)
        return new.model_dump()

    # ------------------------------------------------------------------ health
    def summarizer_state(self) -> str:
        if not self.settings.get().summarizer.enabled:
            return "disabled"
        state = self.cli.info.state
        if state in ("cli_not_found", "unsupported"):
            return "cli_not_found"
        if state == "cli_auth_required":
            return "cli_auth_required"
        return "ready"

    def health(self) -> dict[str, Any]:
        engine = self.stt_worker.engine
        stt = engine.health()
        stt.update(self.model_manager.describe(engine.model or self.settings.get().stt.model))
        stt["engine"] = engine.name
        if isinstance(engine, UnavailableStt):
            stt["note"] = "faster-whisper がインストールされていません（stt エクストラ）。録音は可能です。"
        summarizer_state = self.summarizer_state()
        backlog = sum(self.stt_worker.backlog_ms(mid) for mid in self.meetings.sessions)
        status = self.status
        if status == "ok" and (stt["state"] in ("unavailable", "failed", "not_downloaded")
                               or summarizer_state in ("cli_not_found", "cli_auth_required")):
            status = "degraded"
        return {
            "status": status,
            "apiVersion": API_VERSION,
            "version": __version__,
            "stt": stt,
            "vad": dict(self.vad_info),
            "summarizer": {
                "state": summarizer_state,
                "cliVersion": self.cli.info.version,
                "checking": not self._cli_detected,
                "destination": "Claude Code CLI (claude -p)",
            },
            "queue": {"sttBacklogMs": backlog, "pendingJobs": self.repo.count_active_jobs()},
            "audio": dict(self.audio_notes),
        }

    def privacy(self) -> dict[str, Any]:
        rows = self.repo.db.query("SELECT id, settings_json FROM meetings WHERE state NOT IN ('deleted','deleting')")
        from .util import loads

        retain = 0
        external = 0
        for r in rows:
            st = loads(r["settings_json"], {}) or {}
            if st.get("retainAudio"):
                retain += 1
            if st.get("summarizationEnabled") and \
                    self.repo.consent_state(r["id"])["external_processing"] == "granted":
                external += 1
        ok, reason = self.cipher.available()
        return {
            "storage": {"dataDir": str(self.config.data_dir), "dbPath": str(self.config.db_path)},
            "audioArchive": {
                "enabledMeetings": retain,
                "encryption": "AES-256-GCM（会議ごとの鍵を OS の資格情報ストアに保存）",
                "available": ok,
                "unavailableReason": reason,
            },
            "externalProcessing": {"enabledMeetings": external, "destination": "Claude Code CLI (claude -p)"},
            "notes": [
                "OS のバックアップや利用者自身が作成したコピーはアプリから削除できません。",
                "ログには会議本文・音声・CLI 入出力を記録しません。",
            ],
        }
