"""Realtime STT queue. One worker thread runs the engine; final requests take priority over partials,
and partials are dropped while finals are waiting (latency over completeness for previews).
Backlog is tracked in milliseconds of queued audio per meeting (spec §9 STT 遅延)."""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ..logsetup import log
from ..stt.base import SttEngine, SttSegment

logger = logging.getLogger("sekretaer.stt.worker")


@dataclass(order=True)
class _Item:
    priority: int
    seq: int
    task: SttTask = field(compare=False)


@dataclass(eq=False)
class SttTask:
    meeting_id: str
    source_id: str
    segment_id: str
    audio: np.ndarray
    start_ms: int
    end_ms: int
    partial: bool
    language: str | None
    callback: Callable[[SttTask, list[SttSegment] | None, BaseException | None], Awaitable[None]]
    generation: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def audio_ms(self) -> int:
        return int(self.audio.size * 1000 / 16_000)


class SttWorker:
    def __init__(self, engine: SttEngine) -> None:
        self.engine = engine
        self._queue: asyncio.PriorityQueue[_Item] = asyncio.PriorityQueue()
        self._seq = itertools.count()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt")
        self._task: asyncio.Task[None] | None = None
        self._backlog_ms: dict[str, int] = {}
        self._pending_final: dict[str, list[SttTask]] = {}
        self._idle: dict[str, asyncio.Event] = {}
        self._canceled_meetings: set[str] = set()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="stt-worker")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110
                pass
            self._task = None
        self._executor.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------ submission
    def submit(self, task: SttTask) -> bool:
        mid = task.meeting_id
        if task.partial:
            if self._pending_final.get(mid):
                return False  # finals waiting: skip the partial
            self._queue.put_nowait(_Item(1, next(self._seq), task))
            return True
        self._canceled_meetings.discard(mid)
        self._pending_final.setdefault(mid, []).append(task)
        self._backlog_ms[mid] = self._backlog_ms.get(mid, 0) + task.audio_ms
        self._idle_event(mid).clear()
        self._queue.put_nowait(_Item(0, next(self._seq), task))
        return True

    def _idle_event(self, meeting_id: str) -> asyncio.Event:
        ev = self._idle.get(meeting_id)
        if ev is None:
            ev = asyncio.Event()
            ev.set()
            self._idle[meeting_id] = ev
        return ev

    def backlog_ms(self, meeting_id: str) -> int:
        return self._backlog_ms.get(meeting_id, 0)

    def pending_until(self, meeting_id: str) -> int | None:
        """End ms of the earliest segment still waiting for final STT."""
        pending = self._pending_final.get(meeting_id)
        if not pending:
            return None
        return min(t.end_ms for t in pending)

    def pending_count(self, meeting_id: str) -> int:
        return len(self._pending_final.get(meeting_id, []))

    async def drain(self, meeting_id: str, timeout: float | None = None) -> bool:
        ev = self._idle_event(meeting_id)
        if ev.is_set():
            return True
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False

    def cancel_meeting(self, meeting_id: str) -> None:
        self._canceled_meetings.add(meeting_id)
        self._pending_final.pop(meeting_id, None)
        self._backlog_ms.pop(meeting_id, None)
        self._idle_event(meeting_id).set()

    # ------------------------------------------------------------------ worker
    async def _loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            item = await self._queue.get()
            task = item.task
            if task.meeting_id in self._canceled_meetings:
                continue
            result: list[SttSegment] | None = None
            error: BaseException | None = None
            try:
                result = await loop.run_in_executor(
                    self._executor, lambda t=task: self.engine.transcribe(t.audio, t.language, partial=t.partial)
                )
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001 - reported through the callback
                error = exc
                log(logger, logging.WARNING, "stt failed", meetingId=task.meeting_id, reason=type(exc).__name__,
                    partial=task.partial)
            if not task.partial:
                self._complete_final(task)
            if task.meeting_id in self._canceled_meetings:
                continue
            try:
                await task.callback(task, result, error)
            except Exception:  # noqa: BLE001
                logger.exception("stt callback failed")
            if not task.partial and not self._pending_final.get(task.meeting_id):
                self._idle_event(task.meeting_id).set()

    def _complete_final(self, task: SttTask) -> None:
        mid = task.meeting_id
        pending = self._pending_final.get(mid, [])
        if task in pending:
            pending.remove(task)
            self._backlog_ms[mid] = max(0, self._backlog_ms.get(mid, 0) - task.audio_ms)
