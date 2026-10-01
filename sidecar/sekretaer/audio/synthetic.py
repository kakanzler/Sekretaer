"""File and synthetic audio sources used by tests, fixtures and ``--dev`` demos.

Sources can run faster than realtime (``speed=None``) so a long meeting can be simulated in seconds;
all pipeline timing is derived from sample counts, not wall-clock, so time compression is exact.
"""

from __future__ import annotations

import asyncio
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .base import (
    AudioBackend,
    AudioFrame,
    AudioSource,
    DeviceDisconnected,
    DeviceInfo,
    PermissionDenied,
    SourceUnavailable,
)

# ---------------------------------------------------------------- signal generators


def speech_like(duration_s: float, sr: int = 16_000, seed: int = 0, level: float = 0.25) -> np.ndarray:
    """Voiced, amplitude-modulated harmonic signal that energy and Silero VADs treat as speech-like."""
    rng = np.random.default_rng(seed)
    n = int(duration_s * sr)
    t = np.arange(n) / sr
    f0 = 140 + 30 * np.sin(2 * np.pi * 0.7 * t)
    phase = 2 * np.pi * np.cumsum(f0) / sr
    sig = sum((0.6 / k) * np.sin(k * phase) for k in range(1, 6))
    syll = 0.55 + 0.45 * np.abs(np.sin(2 * np.pi * 3.2 * t))
    noise = rng.normal(0, 0.05, n)
    out = (sig * syll + noise) * level
    return out.astype(np.float32)


def silence(duration_s: float, sr: int = 16_000, noise_level: float = 0.0, seed: int = 1) -> np.ndarray:
    n = int(duration_s * sr)
    if noise_level <= 0:
        return np.zeros(n, dtype=np.float32)
    rng = np.random.default_rng(seed)
    return (rng.normal(0, noise_level, n)).astype(np.float32)


def sine(duration_s: float, freq: float = 440.0, sr: int = 16_000, level: float = 0.2) -> np.ndarray:
    t = np.arange(int(duration_s * sr)) / sr
    return (level * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def meeting_pattern(pattern: list[tuple[str, float]], sr: int = 16_000, noise_level: float = 0.0) -> np.ndarray:
    """Build audio from ``[("speech", 4.0), ("silence", 2.0), ...]``."""
    parts = []
    for i, (kind, dur) in enumerate(pattern):
        if kind == "speech":
            parts.append(speech_like(dur, sr, seed=i))
        elif kind == "silence":
            parts.append(silence(dur, sr, noise_level, seed=i))
        elif kind == "sine":
            parts.append(sine(dur, sr=sr))
        else:
            raise ValueError(kind)
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)


def write_wav(path: Path, samples: np.ndarray, sr: int = 16_000) -> None:
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def read_wav(path: Path) -> tuple[np.ndarray, int, int]:
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        width = w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise SourceUnavailable("only 16-bit PCM WAV is supported")
    data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    return data.reshape(-1, ch), sr, ch


# ---------------------------------------------------------------- sources


@dataclass
class ArraySourceSpec:
    samples: np.ndarray
    sample_rate: int = 16_000
    block_ms: int = 100
    speed: float | None = None  # None = as fast as possible; 1.0 = realtime
    drop_blocks: set[int] = field(default_factory=set)  # block indices lost in transit -> audio.gap
    disconnect_at_block: int | None = None
    disconnect_skip_blocks: int = 20  # blocks lost while "unplugged"
    hold_open: bool = False  # after the data ends, keep delivering silence until stopped
    permission: str = "granted"


class ArraySource(AudioSource):
    continuous_clock = True  # sample indices stay on one clock across reconnects
    fixed_offset_ms = 0

    def __init__(self, kind: str, device_key: str, spec: ArraySourceSpec) -> None:
        self.kind = kind
        self.device_key = device_key
        self.spec = spec
        data = np.asarray(spec.samples, dtype=np.float32)
        self._data = data.reshape(-1, 1) if data.ndim == 1 else data
        self.sample_rate = spec.sample_rate
        self.channels = self._data.shape[1]
        self._block = max(1, int(spec.sample_rate * spec.block_ms / 1000))
        self._i = 0
        self._stopped = False
        self._t0 = 0.0
        self._disconnected = False

    async def start(self) -> None:
        if self.spec.permission == "denied":
            raise PermissionDenied(self.device_key)
        if self.spec.permission == "unavailable":
            raise SourceUnavailable(self.device_key)
        self._stopped = False
        if not self._t0:
            self._t0 = time.monotonic()

    async def read(self) -> AudioFrame | None:
        while True:
            if self._stopped:
                return None
            block_index = self._i
            start = block_index * self._block
            if start >= self._data.shape[0]:
                if not self.spec.hold_open:
                    return None
                chunk = np.zeros((self._block, self.channels), dtype=np.float32)
            else:
                chunk = self._data[start : start + self._block]
            if self.spec.disconnect_at_block is not None and block_index == self.spec.disconnect_at_block \
                    and not self._disconnected:
                self._disconnected = True
                self._i += self.spec.disconnect_skip_blocks
                raise DeviceDisconnected(self.device_key)
            self._i += 1
            if self.spec.speed:
                due = self._t0 + (start / self.sample_rate) / self.spec.speed
                delay = due - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
            else:
                await asyncio.sleep(0)
            if block_index in self.spec.drop_blocks:
                continue
            return AudioFrame(data=chunk, start_sample=start)

    async def stop(self) -> None:
        self._stopped = True


class SyntheticBackend(AudioBackend):
    """Backend for ``synthetic:`` and ``file:`` device keys."""

    name = "synthetic"

    def __init__(self, sources: dict[str, Callable[[], ArraySourceSpec]] | None = None,
                 kinds: dict[str, str] | None = None) -> None:
        self.sources: dict[str, Callable[[], ArraySourceSpec]] = dict(sources or {})
        self.kinds: dict[str, str] = dict(kinds or {})

    def add(self, key: str, factory: Callable[[], ArraySourceSpec], kind: str = "microphone") -> None:
        self.sources[key] = factory
        self.kinds[key] = kind

    def handles(self, device_key: str) -> bool:
        return device_key in self.sources or device_key.startswith("file:")

    def list_devices(self) -> list[DeviceInfo]:
        out = []
        for key, factory in self.sources.items():
            spec = factory()
            out.append(DeviceInfo(key=key, name=f"Synthetic ({key})", kind=self.kinds.get(key, "microphone"),
                                  defaultSampleRate=spec.sample_rate, channels=1,
                                  available=spec.permission != "unavailable", note="synthetic test source"))
        return out

    def probe(self, kind: str, device_key: str) -> str:
        if device_key.startswith("file:"):
            return "granted" if Path(device_key[5:]).is_file() else "unavailable"
        factory = self.sources.get(device_key)
        if factory is None:
            return "unavailable"
        return factory().permission

    def open(self, kind: str, device_key: str) -> AudioSource:
        if device_key.startswith("file:"):
            data, sr, _ch = read_wav(Path(device_key[5:]))
            return ArraySource(kind, device_key, ArraySourceSpec(samples=data, sample_rate=sr))
        factory = self.sources.get(device_key)
        if factory is None:
            raise SourceUnavailable(device_key)
        return ArraySource(kind, device_key, factory())


def demo_backend() -> SyntheticBackend:
    """Realtime synthetic devices for UI development without a microphone (``--dev --synthetic-audio``)."""
    pattern = []
    for _ in range(40):
        pattern += [("speech", 6.0), ("silence", 2.5), ("speech", 9.0), ("silence", 1.6)]
    audio = meeting_pattern(pattern)
    backend = SyntheticBackend()
    backend.add("synthetic:demo-mic", lambda: ArraySourceSpec(samples=audio, speed=1.0, hold_open=True))
    backend.add("synthetic:demo-system",
                lambda: ArraySourceSpec(samples=audio[::-1].copy(), speed=1.0, hold_open=True), kind="system")
    return backend
