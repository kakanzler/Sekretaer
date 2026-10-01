"""Fixed-capacity ring buffer over the 16 kHz derived stream with absolute sample indexing.

The writer (capture) never blocks. If the reader (VAD/STT feeder) falls more than ``capacity``
samples behind, the oldest unread samples are overwritten and :meth:`read_available` reports how
many samples were lost so the caller can record an ``audio.gap`` (spec §4: 30 s initial size).
"""

from __future__ import annotations

import numpy as np


class RingBuffer:
    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._buf = np.zeros(capacity, dtype=np.float32)
        self.write_pos = 0  # absolute index of the next sample to be written
        self.read_pos = 0  # absolute index of the next sample the reader wants

    @property
    def oldest(self) -> int:
        return max(0, self.write_pos - self.capacity)

    def write(self, samples: np.ndarray) -> None:
        x = np.asarray(samples, dtype=np.float32)
        n = x.size
        if n == 0:
            return
        if n >= self.capacity:
            x = x[-self.capacity :]
            skipped = n - self.capacity
            self.write_pos += skipped
            n = self.capacity
        start = self.write_pos % self.capacity
        first = min(n, self.capacity - start)
        self._buf[start : start + first] = x[:first]
        if first < n:
            self._buf[: n - first] = x[first:]
        self.write_pos += n

    def write_silence(self, n: int) -> None:
        if n <= 0:
            return
        if n >= self.capacity:
            self._buf[:] = 0.0
            self.write_pos += n
            return
        self.write(np.zeros(n, dtype=np.float32))

    def get(self, start: int, end: int) -> np.ndarray | None:
        """Copy absolute range [start, end). None if any part was already overwritten."""
        if start < self.oldest or end > self.write_pos or end < start:
            return None
        n = end - start
        out = np.empty(n, dtype=np.float32)
        s = start % self.capacity
        first = min(n, self.capacity - s)
        out[:first] = self._buf[s : s + first]
        if first < n:
            out[first:] = self._buf[: n - first]
        return out

    def get_clipped(self, start: int, end: int) -> tuple[int, np.ndarray]:
        start = max(start, self.oldest)
        end = min(end, self.write_pos)
        if end <= start:
            return start, np.zeros(0, dtype=np.float32)
        data = self.get(start, end)
        assert data is not None
        return start, data

    def read_available(self, block: int) -> tuple[int, np.ndarray | None, int]:
        """Return (start_index, data, lost_samples) for whole ``block`` multiples ready to read."""
        lost = 0
        if self.read_pos < self.oldest:
            lost = self.oldest - self.read_pos
            self.read_pos = self.oldest
        avail = self.write_pos - self.read_pos
        n = (avail // block) * block
        if n <= 0:
            return self.read_pos, None, lost
        start = self.read_pos
        data = self.get(start, start + n)
        self.read_pos += n
        return start, data, lost
