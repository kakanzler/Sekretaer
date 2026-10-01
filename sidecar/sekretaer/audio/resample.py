"""Conversion of source audio to the 16 kHz mono float32 stream used by VAD and STT (spec §3)."""

from __future__ import annotations

import numpy as np

TARGET_RATE = 16_000
_EMPTY = np.zeros(0, dtype=np.float32)


def to_mono(data: np.ndarray) -> np.ndarray:
    arr = np.asarray(data, dtype=np.float32)
    if arr.ndim == 1:
        return arr
    if arr.shape[1] == 1:
        return arr[:, 0]
    return arr.mean(axis=1, dtype=np.float32)


class StreamingResampler:
    """Linear-interpolation resampler that keeps phase across blocks.

    When down-sampling by an integer ratio (48 k -> 16 k) a moving-average pre-filter reduces aliasing.
    That is adequate for VAD/STT input and keeps the dependency set small.
    """

    def __init__(self, src_rate: int, dst_rate: int = TARGET_RATE) -> None:
        if src_rate <= 0 or dst_rate <= 0:
            raise ValueError("invalid sample rate")
        self.src_rate = src_rate
        self.dst_rate = dst_rate
        self.step = src_rate / dst_rate
        ratio = src_rate / dst_rate
        self._box = int(round(ratio)) if ratio >= 2 else 1
        self._fir_state = np.zeros(max(self._box - 1, 0), dtype=np.float64)
        self._buf = _EMPTY
        self._pos = 0.0

    def _prefilter(self, x: np.ndarray) -> np.ndarray:
        if self._box <= 1 or x.size == 0:
            return x
        ext = np.concatenate([self._fir_state, x.astype(np.float64)])
        c = np.cumsum(np.concatenate([[0.0], ext]))
        y = (c[self._box :] - c[: -self._box]) / self._box
        self._fir_state = ext[-(self._box - 1) :]
        return y.astype(np.float32)

    def process(self, mono: np.ndarray) -> np.ndarray:
        x = np.asarray(mono, dtype=np.float32)
        if self.src_rate == self.dst_rate:
            return x.copy()
        x = self._prefilter(x)
        buf = np.concatenate([self._buf, x]) if self._buf.size else x
        if buf.size < 2:
            self._buf = buf
            return _EMPTY
        last = buf.size - 1
        if self._pos > last:
            self._buf = buf
            return _EMPTY
        n_out = int(np.floor((last - self._pos) / self.step)) + 1
        idx = self._pos + np.arange(n_out, dtype=np.float64) * self.step
        lo = np.floor(idx).astype(np.int64)
        frac = (idx - lo).astype(np.float32)
        hi = np.minimum(lo + 1, last)
        out = buf[lo] * (1.0 - frac) + buf[hi] * frac
        next_pos = self._pos + n_out * self.step
        keep = min(int(np.floor(next_pos)), last)
        self._buf = buf[keep:]
        self._pos = next_pos - keep
        return out.astype(np.float32)


def resample_once(mono: np.ndarray, src_rate: int, dst_rate: int = TARGET_RATE) -> np.ndarray:
    return StreamingResampler(src_rate, dst_rate).process(mono)
