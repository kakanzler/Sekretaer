"""Archive queue (separate from the realtime STT path, spec §3).

Receives the 16 kHz derived stream in blocks and cuts it into 30 s chunks. Every chunk gets an
``audio_chunks`` row so continuity can be audited. With retention OFF (default) no audio is written
and the row keeps neither path nor hash; with retention ON the chunk is AES-GCM encrypted with the
meeting key and written under ``<data>/audio/<meeting>/``. A full archive queue drops blocks and the
drop is reported as a gap.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

import numpy as np

from ..logsetup import log
from ..privacy.crypto import AudioCipher
from ..util import sha256_hex

logger = logging.getLogger("sekretaer.archive")
SR = 16_000
CHUNK_SAMPLES = 30 * SR


class ArchiveWriter:
    def __init__(
        self, *, meeting_id: str, source_id: str, directory: Path, cipher: AudioCipher | None,
        retain: Callable[[], bool], to_ms: Callable[[int], int], record_chunk: Callable[..., None],
        on_drop: Callable[[int, int], None], maxsize: int = 600,
    ) -> None:
        self.meeting_id = meeting_id
        self.source_id = source_id
        self.directory = directory
        self.cipher = cipher
        self.retain = retain
        self.to_ms = to_ms
        self.record_chunk = record_chunk
        self.on_drop = on_drop
        self.queue: asyncio.Queue[tuple[int, np.ndarray] | None] = asyncio.Queue(maxsize=maxsize)
        self._parts: list[np.ndarray] = []
        self._start: int | None = None
        self._next: int | None = None
        self._count = 0
        self._task: asyncio.Task[None] | None = None
        self.files_written = 0

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"archive-{self.source_id}")

    def put(self, start_index: int, block: np.ndarray) -> None:
        try:
            self.queue.put_nowait((start_index, block))
        except asyncio.QueueFull:
            self.on_drop(start_index, block.size)

    async def close(self) -> None:
        if self._task is None:
            return
        await self.queue.put(None)
        await self._task
        self._task = None

    async def _run(self) -> None:
        while True:
            item = await self.queue.get()
            if item is None:
                await self._flush()
                return
            idx, block = item
            if self._next is not None and idx != self._next:
                await self._flush()
            if self._start is None:
                self._start = idx
            self._parts.append(block)
            self._count += block.size
            self._next = idx + block.size
            if self._count >= CHUNK_SAMPLES:
                await self._flush()

    async def _flush(self) -> None:
        if self._start is None or self._count == 0:
            self._parts, self._start, self._count = [], None, 0
            return
        start, count, parts = self._start, self._count, self._parts
        self._parts, self._start, self._count = [], None, 0
        start_ms, end_ms = self.to_ms(start), self.to_ms(start + count)
        if self.retain() and self.cipher is not None:
            try:
                pcm = (np.clip(np.concatenate(parts), -1, 1) * 32767).astype("<i2").tobytes()
                blob = await asyncio.to_thread(self.cipher.encrypt, self.meeting_id, pcm)
                self.directory.mkdir(parents=True, exist_ok=True)
                path = self.directory / f"{self.source_id}-{start_ms:010d}.ska"
                await asyncio.to_thread(path.write_bytes, blob)
                self.files_written += 1
                self.record_chunk(start_ms=start_ms, end_ms=end_ms, status="stored", storage_path=str(path),
                                  sha256=sha256_hex(blob))
                return
            except Exception as exc:  # noqa: BLE001
                log(logger, logging.ERROR, "archive write failed", meetingId=self.meeting_id,
                    reason=type(exc).__name__)
        self.record_chunk(start_ms=start_ms, end_ms=end_ms, status="discarded", storage_path=None, sha256=None)
