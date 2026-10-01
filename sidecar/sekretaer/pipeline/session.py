"""Live meeting pipeline: per-source capture -> 30 s ring buffer (16 kHz mono) -> VAD/segmenter -> STT
queue -> transcript segments -> breakpoint detection -> persistent summary jobs. The archive path runs on
its own queue. Timing is derived from sample counts so synthetic sources can run faster than realtime.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

from ..audio.archive import ArchiveWriter
from ..audio.base import AudioFrame, AudioSource, AudioSourceError, DeviceDisconnected
from ..audio.resample import TARGET_RATE, StreamingResampler, to_mono
from ..audio.ringbuffer import RingBuffer
from ..logsetup import log
from ..stt.base import SttSegment, SttUnavailable
from ..util import new_id
from ..vad.models import VadModel
from ..vad.segmenter import Segmenter, SpeechEnd, SpeechPartial, SpeechStart
from .breakpoints import BreakpointDetector, BreakpointPolicy
from .stt_worker import SttTask

if TYPE_CHECKING:  # pragma: no cover
    from .meetings import MeetingService

logger = logging.getLogger("sekretaer.session")

RING_SECONDS = 30
FRAME = 512
BACKLOG_WARN_MS = 15_000
BACKLOG_CLEAR_MS = 5_000
LEVEL_INTERVAL_S = 0.1


@dataclass
class SessionTuning:
    reconnect_delay_s: float = 2.0
    backlog_warn_ms: int = BACKLOG_WARN_MS
    backlog_clear_ms: int = BACKLOG_CLEAR_MS
    manual_drain_timeout_s: float = 15.0


class SourcePipeline:
    def __init__(self, session: MeetingSession, row: dict[str, Any], adapter: AudioSource, vad: VadModel,
                 threshold: float) -> None:
        self.session = session
        self.row = row
        self.source_id: str = row["id"]
        self.kind: str = row["kind"]
        self.adapter = adapter
        self.vad = vad
        self.segmenter = Segmenter(threshold)
        self.ring = RingBuffer(RING_SECONDS * TARGET_RATE)
        self.resampler = StreamingResampler(adapter.sample_rate, TARGET_RATE)
        self.offset_ms = 0
        self.expected: int | None = None  # next expected source sample index
        self.wake = asyncio.Event()
        self.capture_done = False
        self.processed = 0  # 16 kHz samples consumed by VAD
        self.current_segment: str | None = None
        self.segment_start = 0
        self._last_level = 0.0
        self._disconnected_at: float | None = None
        self._gap_reason_hint: str | None = None
        self.capture_task: asyncio.Task[None] | None = None
        self.consume_task: asyncio.Task[None] | None = None
        self.archive: ArchiveWriter | None = None

    # time mapping --------------------------------------------------------
    def ms(self, index16: int) -> int:
        return self.offset_ms + int(index16 * 1000 // TARGET_RATE)

    @property
    def processed_ms(self) -> int:
        return self.ms(self.processed)

    # capture -------------------------------------------------------------
    async def capture_loop(self) -> None:
        s = self.session
        while not s.stopping:
            try:
                frame = await self.adapter.read()
            except DeviceDisconnected:
                if not await self._reconnect():
                    break
                continue
            except AudioSourceError:
                break
            if frame is None:
                break
            self._ingest(frame)
            if s.halted:
                break
        self.capture_done = True
        self.wake.set()

    async def _reconnect(self) -> bool:
        s = self.session
        self._disconnected_at = time.monotonic()
        at_ms = self.ms(self.ring.write_pos)
        s.on_source_lost(self, at_ms)
        while not s.stopping:
            await asyncio.sleep(s.tuning.reconnect_delay_s)
            if s.stopping:
                break
            try:
                await self.adapter.stop()
                await self.adapter.start()
            except AudioSourceError:
                continue
            lost_ms = int((time.monotonic() - self._disconnected_at) * 1000)
            if not getattr(self.adapter, "continuous_clock", False):
                # New stream, new clock: account for the outage by wall time.
                self.expected = None
                self._fill_gap(int(lost_ms * TARGET_RATE / 1000), "device_disconnected")
            else:
                # Same clock: the sample-index jump of the next frame measures the outage.
                self._gap_reason_hint = "device_disconnected"
            self._disconnected_at = None
            s.on_source_restored(self)
            return True
        # Stopped while disconnected: the rest of the meeting for this source is missing.
        lost_ms = int((time.monotonic() - self._disconnected_at) * 1000)
        s.report_gap(self, at_ms, lost_ms, "device_disconnected")
        return False

    def _fill_gap(self, n16: int, reason: str) -> None:
        if n16 <= 0:
            return
        at_ms = self.ms(self.ring.write_pos)
        self.ring.write_silence(n16)
        self.session.report_gap(self, at_ms, int(n16 * 1000 / TARGET_RATE), reason)

    def _ingest(self, frame: AudioFrame) -> None:
        data = np.asarray(frame.data, dtype=np.float32)
        if data.ndim == 1:
            data = data.reshape(-1, 1)
        n = data.shape[0]
        sr = self.adapter.sample_rate
        if frame.start_sample is not None and self.expected is not None and frame.start_sample > self.expected:
            missing = frame.start_sample - self.expected
            reason = self._gap_reason_hint or "input_discontinuity"
            self._fill_gap(int(round(missing * TARGET_RATE / sr)), reason)
        elif frame.status == "input_overflow":
            self.session.report_gap(self, self.ms(self.ring.write_pos), 0, "input_overflow")
        self._gap_reason_hint = None
        if frame.start_sample is not None:
            self.expected = frame.start_sample + n
        elif self.expected is not None:
            self.expected += n
        mono = to_mono(data)
        x16 = self.resampler.process(mono)
        if x16.size == 0:
            return
        start_index = self.ring.write_pos
        self.ring.write(x16)
        if self.archive is not None:
            self.archive.put(start_index, x16)
        self._level(x16)
        self.wake.set()

    def _level(self, x16: np.ndarray) -> None:
        now = time.monotonic()
        if now - self._last_level < LEVEL_INTERVAL_S:
            return
        self._last_level = now
        rms = float(np.sqrt(np.mean(np.square(x16, dtype=np.float64)))) if x16.size else 0.0
        peak = float(np.max(np.abs(x16))) if x16.size else 0.0
        db = float(20 * np.log10(rms + 1e-10))
        self.session.publish("audio.level", {"sourceId": self.source_id, "source": self.kind,
                                             "rms": round(rms, 5), "peak": round(peak, 5), "db": round(db, 1)})

    # consume -------------------------------------------------------------
    async def consume_loop(self) -> None:
        s = self.session
        while True:
            await self.wake.wait()
            self.wake.clear()
            while True:
                start, data, lost = self.ring.read_available(FRAME * 8)
                if lost:
                    self._handle_ring_overflow(start, lost)
                if data is None:
                    # Drain the remainder in single frames (the 8-frame batch is only an optimisation).
                    start, data, lost = self.ring.read_available(FRAME)
                    if data is None:
                        break
                frames = data.reshape(-1, FRAME)
                probs = self.vad.probs(frames)
                for ev in self.segmenter.process(start, probs):
                    self._on_vad_event(ev)
                self.processed = start + data.size
                s.evaluate_breakpoints()
                await asyncio.sleep(0)
            if self.capture_done:
                break
        for ev in self.segmenter.flush(self.ring.write_pos):
            self._on_vad_event(ev)
        self.processed = self.ring.write_pos
        s.evaluate_breakpoints()

    def _handle_ring_overflow(self, start: int, lost: int) -> None:
        at = start - lost
        self.session.report_gap(self, self.ms(at), int(lost * 1000 / TARGET_RATE), "ring_buffer_overflow")
        if self.segmenter.in_speech:
            for ev in self.segmenter.flush(at):
                self._on_vad_event(ev)
        self.segmenter.floor = start

    def _on_vad_event(self, ev: object) -> None:
        s = self.session
        if isinstance(ev, SpeechStart):
            self.current_segment = new_id()
            self.segment_start = ev.start
            s.detector.on_speech_start(self.source_id, self.ms(ev.speech_start))
        elif isinstance(ev, SpeechPartial):
            if self.current_segment is None:
                return
            audio = self.ring.get_clipped(ev.start, ev.end)[1]
            s.submit_stt(self, self.current_segment, ev.start, ev.end, audio, partial=True)
        elif isinstance(ev, SpeechEnd):
            seg_id = self.current_segment or new_id()
            self.current_segment = None
            start, audio = self.ring.get_clipped(ev.start, ev.end)
            s.submit_stt(self, seg_id, start, start + audio.size, audio, partial=False)
            s.detector.on_speech_end(self.source_id, self.ms(ev.speech_end), self.ms(ev.end))


class MeetingSession:
    def __init__(
        self, *, service: MeetingService, meeting: dict[str, Any], sources: list[tuple[dict[str, Any], AudioSource]],
        vad_factory: Callable[[], tuple[VadModel, float]], policy: BreakpointPolicy | None = None,
        tuning: SessionTuning | None = None,
    ) -> None:
        self.service = service
        self.repo = service.repo
        self.bus = service.bus
        self.stt = service.stt_worker
        self.meeting_id: str = meeting["id"]
        self.language: str | None = None if meeting["language"] == "auto" else meeting["language"]
        self.detector = BreakpointDetector(policy or BreakpointPolicy())
        self.tuning = tuning or SessionTuning()
        self.stopping = False
        self.halted = False  # set after a persistent DB write failure
        self._backlog_flag = False
        self._finalized: set[str] = set()
        self._stt_available = True
        self.pipelines: list[SourcePipeline] = []
        for row, adapter in sources:
            vad, threshold = vad_factory()
            self.pipelines.append(SourcePipeline(self, row, adapter, vad, threshold))
        cursor = self.repo.claimed_cursor_ms(self.meeting_id)
        self.detector.cursor_ms = cursor
        self.detector.last_job_at_ms = cursor

    # lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        engine_state = self.stt.engine.state
        if engine_state in ("unavailable", "not_downloaded", "failed"):
            self._stt_available = False
            self.service.add_degraded(self.meeting_id, "stt_model_unavailable")
            self.warning("stt_model_unavailable", {"sttState": engine_state})
        for p in self.pipelines:
            fixed = getattr(p.adapter, "fixed_offset_ms", None)
            p.offset_ms = int(fixed) if fixed is not None else self.service.elapsed_ms(self.meeting_id)
            self._persist(self.repo.update_source, p.source_id, sample_rate=p.adapter.sample_rate,
                          channels=p.adapter.channels, offset_ms=p.offset_ms)
            p.archive = ArchiveWriter(
                meeting_id=self.meeting_id, source_id=p.source_id,
                directory=self.service.config.meeting_audio_dir(self.meeting_id), cipher=self.service.cipher,
                retain=lambda: self.service.retain_audio(self.meeting_id), to_ms=p.ms,
                record_chunk=lambda pl=p, **kw: self._persist(self.repo.insert_chunk, meeting_id=self.meeting_id,
                                                              source_id=pl.source_id, **kw),
                on_drop=lambda idx, n, pl=p: self.report_gap(pl, pl.ms(idx), int(n * 1000 / TARGET_RATE),
                                                            "archive_queue_overflow"),
            )
            p.archive.start()
            p.capture_task = asyncio.create_task(p.capture_loop(), name=f"capture-{p.source_id}")
            p.consume_task = asyncio.create_task(p.consume_loop(), name=f"consume-{p.source_id}")

    async def stop_and_drain(self) -> None:
        """Stop capture, finish VAD on buffered audio, wait for all pending STT and archive writes."""
        self.stopping = True
        for p in self.pipelines:
            try:
                await p.adapter.stop()
            except Exception:  # noqa: BLE001, S110
                pass
        for p in self.pipelines:
            if p.capture_task is not None:
                try:
                    await asyncio.wait_for(p.capture_task, timeout=10)
                except (TimeoutError, asyncio.CancelledError):
                    p.capture_task.cancel()
                p.capture_done = True
                p.wake.set()
        for p in self.pipelines:
            if p.consume_task is not None:
                await p.consume_task
        await self.stt.drain(self.meeting_id)
        for p in self.pipelines:
            if p.archive is not None:
                await p.archive.close()
        self.evaluate_breakpoints()

    async def abort(self) -> None:
        self.stopping = True
        self.stt.cancel_meeting(self.meeting_id)
        for p in self.pipelines:
            try:
                await p.adapter.stop()
            except Exception:  # noqa: BLE001, S110
                pass
            for t in (p.capture_task, p.consume_task):
                if t is not None:
                    t.cancel()
            if p.archive is not None and p.archive._task is not None:
                p.archive._task.cancel()
        for p in self.pipelines:
            for t in (p.capture_task, p.consume_task, p.archive._task if p.archive else None):
                if t is not None:
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110
                        pass

    # helpers -------------------------------------------------------------
    def publish(self, event: str, data: dict[str, Any]) -> None:
        self.bus.publish(event, self.meeting_id, data)

    def warning(self, code: str, details: dict[str, Any] | None = None) -> None:
        from ..errors import MESSAGES_JA

        self.publish("warning", {"code": code, "message": MESSAGES_JA.get(code, code), "details": details or {}})

    def _persist(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        for attempt in range(2):
            try:
                return fn(*args, **kwargs)
            except sqlite3.Error:
                if attempt == 0:
                    time.sleep(0.05)
                    continue
                self._on_db_failure()
                return None
        return None

    def _on_db_failure(self) -> None:
        if self.halted:
            return
        self.halted = True
        log(logger, logging.ERROR, "db write failed; halting new audio processing", meetingId=self.meeting_id,
            code="db_write_failed")
        self.service.add_degraded(self.meeting_id, "db_write_failed", persist=False)
        self.warning("db_write_failed", {"action": "stop_recording_suggested"})
        try:
            self.repo.insert_issue(meeting_id=self.meeting_id, kind="db_write_failed")
        except sqlite3.Error:
            pass

    def report_gap(self, p: SourcePipeline, at_ms: int, gap_ms: int, reason: str) -> None:
        self._persist(self.repo.insert_chunk, meeting_id=self.meeting_id, source_id=p.source_id, start_ms=at_ms,
                      end_ms=at_ms + max(0, gap_ms), status="gap")
        self._persist(self.repo.insert_issue, meeting_id=self.meeting_id, kind="audio_gap", source_id=p.source_id,
                      at_ms=at_ms, length_ms=gap_ms, details={"reason": reason})
        self.publish("audio.gap", {"sourceId": p.source_id, "source": p.kind, "atMs": at_ms, "gapMs": gap_ms,
                                   "reason": reason})
        log(logger, logging.WARNING, "audio gap", meetingId=self.meeting_id, sourceId=p.source_id, atMs=at_ms,
            gapMs=gap_ms, reason=reason)

    def on_source_lost(self, p: SourcePipeline, at_ms: int) -> None:
        self._persist(self.repo.insert_issue, meeting_id=self.meeting_id, kind="device_disconnected",
                      source_id=p.source_id, at_ms=at_ms)
        self.service.add_degraded(self.meeting_id, "source_unavailable")
        self.warning("source_unavailable", {"sourceId": p.source_id, "reason": "device_disconnected", "atMs": at_ms})

    def on_source_restored(self, p: SourcePipeline) -> None:
        if all(q._disconnected_at is None for q in self.pipelines):
            self.service.remove_degraded(self.meeting_id, "source_unavailable")

    # STT -----------------------------------------------------------------
    def submit_stt(self, p: SourcePipeline, seg_id: str, start: int, end: int, audio: np.ndarray, *,
                   partial: bool) -> None:
        if not self._stt_available or audio.size < FRAME or self.halted:
            return
        task = SttTask(meeting_id=self.meeting_id, source_id=p.source_id, segment_id=seg_id, audio=audio,
                       start_ms=p.ms(start), end_ms=p.ms(end), partial=partial, language=self.language,
                       callback=self._on_stt_result, meta={"kind": p.kind})
        if self.stt.submit(task) and not partial:
            self._check_backlog()

    def _check_backlog(self) -> None:
        backlog = self.stt.backlog_ms(self.meeting_id)
        if not self._backlog_flag and backlog > self.tuning.backlog_warn_ms:
            self._backlog_flag = True
            self._persist(self.repo.insert_issue, meeting_id=self.meeting_id, kind="stt_queue_backlog",
                          details={"backlogMs": backlog})
            self.service.add_degraded(self.meeting_id, "stt_queue_backlog")
            self.warning("stt_queue_backlog", {"backlogMs": backlog})
        elif self._backlog_flag and backlog < self.tuning.backlog_clear_ms:
            self._backlog_flag = False
            self.service.remove_degraded(self.meeting_id, "stt_queue_backlog")

    async def _on_stt_result(self, task: SttTask, result: list[SttSegment] | None,
                             error: BaseException | None) -> None:
        if error is not None:
            if isinstance(error, SttUnavailable) and self._stt_available:
                self._stt_available = False
                self.service.add_degraded(self.meeting_id, "stt_model_unavailable")
                self.warning("stt_model_unavailable", {})
            if not task.partial:
                self._check_backlog()
                self.evaluate_breakpoints()
            return
        segs = result or []
        kind = task.meta.get("kind", "microphone")
        if task.partial:
            if task.segment_id in self._finalized or not segs:
                return
            text = " ".join(s.text for s in segs).strip()
            self._persist(self.repo.upsert_partial_segment, segment_id=task.segment_id, meeting_id=self.meeting_id,
                          source_id=task.source_id, start_ms=task.start_ms, end_ms=task.end_ms, text=text,
                          language=segs[0].language)
            self.publish("transcript.partial", {"segmentId": task.segment_id, "sourceId": task.source_id,
                                                "source": kind, "startMs": task.start_ms, "endMs": task.end_ms,
                                                "text": text, "isFinal": False, "revision": 0})
            return
        self._finalized.add(task.segment_id)
        if not segs:
            self._persist(self.repo.delete_segment, task.segment_id)
            self.publish("transcript.partial", {"segmentId": task.segment_id, "sourceId": task.source_id,
                                                "source": kind, "startMs": task.start_ms, "endMs": task.end_ms,
                                                "text": "", "isFinal": False, "revision": 0, "removed": True})
        for i, seg in enumerate(segs):
            seg_id = task.segment_id if i == 0 else new_id()
            start_ms = task.start_ms + int(seg.start_s * 1000)
            end_ms = min(task.end_ms, task.start_ms + int(round(seg.end_s * 1000)))
            end_ms = max(end_ms, start_ms)
            meta = {"avgLogprob": seg.avg_logprob, "noSpeechProb": seg.no_speech_prob,
                    "engine": self.stt.engine.name, "model": self.stt.engine.model}
            self._persist(self.repo.finalize_segment, segment_id=seg_id, meeting_id=self.meeting_id,
                          source_id=task.source_id, start_ms=start_ms, end_ms=end_ms, text=seg.text,
                          language=seg.language, stt_metadata=meta)
            self.publish("transcript.final", {"segmentId": seg_id, "sourceId": task.source_id, "source": kind,
                                              "startMs": start_ms, "endMs": end_ms, "text": seg.text,
                                              "language": seg.language, "isFinal": True, "revision": 1})
            self.detector.on_final(start_ms, end_ms, seg.text)
        self._check_backlog()
        self.evaluate_breakpoints()

    # breakpoints ---------------------------------------------------------
    def audio_now_ms(self) -> int:
        live = [p for p in self.pipelines if not p.capture_done or p.processed < p.ring.write_pos]
        pool = live or self.pipelines
        if not pool:
            return 0
        return min(p.processed_ms for p in pool) if live else max(p.processed_ms for p in pool)

    def evaluate_breakpoints(self) -> None:
        if self.halted:
            return
        now_ms = self.audio_now_ms()
        trig = self.detector.evaluate(now_ms, self.stt.pending_until(self.meeting_id))
        if trig is None:
            return
        self.detector.on_job_created(trig.range_end_ms, now_ms)
        self.service.create_auto_job(self.meeting_id, trig.kind, trig.range_end_ms)

    async def prepare_manual(self) -> int:
        """Wait (bounded) for in-flight STT so the manual job starts at a finalized boundary."""
        await self.stt.drain(self.meeting_id, timeout=self.tuning.manual_drain_timeout_s)
        return self.audio_now_ms()
