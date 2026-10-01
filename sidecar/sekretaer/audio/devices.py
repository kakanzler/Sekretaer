"""Live capture adapters. Both are optional (``audio`` extra) and imported lazily:

* ``sd:<name>``              microphone via sounddevice/PortAudio
* ``wasapi-loopback:<name>`` Windows system audio via PyAudioWPatch (WASAPI loopback)
* ``system:default``          placeholder on platforms without a system-audio adapter (always unavailable)

OS permission prompts are owned by the Tauri shell; adapters only report what the driver says and
never try to work around a denial (spec §4).
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Any

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

_BLOCK_S = 0.1
_QUEUE_BLOCKS = 300  # 30 s of 100 ms blocks between the driver thread and the event loop
_STALL_S = 3.0


def _permission_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(k in text for k in ("permission", "access denied", "not authorized", "unauthorized", "-9999"))


class _CallbackSource(AudioSource):
    """Bridges a driver callback thread to asyncio with drop-and-count overflow handling."""

    def __init__(self, kind: str, device_key: str) -> None:
        self.kind = kind
        self.device_key = device_key
        self.sample_rate = 16_000
        self.channels = 1
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[AudioFrame] | None = None
        self._counter = 0
        self._stopped = False
        self._last_frame_at = 0.0

    def _emit(self, data: np.ndarray, overflow: bool) -> None:
        start = self._counter
        self._counter += data.shape[0]
        frame = AudioFrame(data=data, start_sample=start, status="input_overflow" if overflow else None)
        loop, queue = self._loop, self._queue
        if loop is None or queue is None or self._stopped:
            return

        def _put() -> None:
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                pass  # dropped: the next frame's start_sample jump is reported as a gap

        loop.call_soon_threadsafe(_put)

    async def read(self) -> AudioFrame | None:
        assert self._queue is not None
        while not self._stopped:
            try:
                frame = await asyncio.wait_for(self._queue.get(), timeout=0.5)
                self._last_frame_at = time.monotonic()
                return frame
            except TimeoutError:
                if not self._stream_alive() or time.monotonic() - self._last_frame_at > _STALL_S:
                    raise DeviceDisconnected(self.device_key) from None
        return None

    def _stream_alive(self) -> bool:  # pragma: no cover - overridden
        return True

    def _prepare_loop(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=_QUEUE_BLOCKS)
        self._last_frame_at = time.monotonic()


# ------------------------------------------------------------------ sounddevice (microphone)


class SoundDeviceSource(_CallbackSource):
    def __init__(self, kind: str, device_key: str, sd: Any, device_index: int | None, info: dict) -> None:
        super().__init__(kind, device_key)
        self._sd = sd
        self._index = device_index
        self.sample_rate = int(info.get("default_samplerate") or 48_000)
        self.channels = max(1, min(2, int(info.get("max_input_channels") or 1)))
        self._stream: Any = None

    async def start(self) -> None:
        self._prepare_loop()

        def cb(indata: np.ndarray, frames: int, _time: Any, status: Any) -> None:
            self._emit(indata.copy(), bool(getattr(status, "input_overflow", False)))

        try:
            self._stream = self._sd.InputStream(
                device=self._index, channels=self.channels, samplerate=self.sample_rate, dtype="float32",
                blocksize=int(self.sample_rate * _BLOCK_S), callback=cb,
            )
            self._stream.start()
        except Exception as exc:  # noqa: BLE001 - PortAudio raises generic errors
            if _permission_error(exc):
                raise PermissionDenied(self.device_key) from exc
            raise SourceUnavailable(self.device_key) from exc

    def _stream_alive(self) -> bool:
        return bool(self._stream is not None and self._stream.active)

    async def stop(self) -> None:
        self._stopped = True
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:  # noqa: BLE001, S110
                pass


class SoundDeviceBackend(AudioBackend):
    name = "sounddevice"
    prefix = "sd:"

    def __init__(self) -> None:
        import sounddevice as sd  # optional dependency

        self.sd = sd

    def handles(self, device_key: str) -> bool:
        return device_key.startswith(self.prefix)

    def _inputs(self) -> list[tuple[int, dict]]:
        return [(i, dict(d)) for i, d in enumerate(self.sd.query_devices()) if int(d.get("max_input_channels", 0)) > 0]

    def _resolve(self, device_key: str) -> tuple[int | None, dict] | None:
        name = device_key[len(self.prefix):]
        if name == "default":
            try:
                info = dict(self.sd.query_devices(kind="input"))
            except Exception:  # noqa: BLE001
                return None
            return None, info
        for idx, info in self._inputs():
            if info.get("name") == name:
                return idx, info
        return None

    def list_devices(self) -> list[DeviceInfo]:
        out = []
        try:
            default = dict(self.sd.query_devices(kind="input"))
            out.append(DeviceInfo(key="sd:default", name=f"既定のマイク ({default.get('name')})", kind="microphone",
                                  defaultSampleRate=int(default.get("default_samplerate") or 0) or None,
                                  channels=int(default.get("max_input_channels") or 1), available=True))
        except Exception:  # noqa: BLE001, S110 - no default input device
            pass
        seen = set()
        for _idx, info in self._inputs():
            name = str(info.get("name"))
            if name in seen:
                continue
            seen.add(name)
            out.append(DeviceInfo(key=f"sd:{name}", name=name, kind="microphone",
                                  defaultSampleRate=int(info.get("default_samplerate") or 0) or None,
                                  channels=int(info.get("max_input_channels") or 1), available=True))
        return out

    def probe(self, kind: str, device_key: str) -> str:
        # PortAudio cannot query OS privacy state without opening a stream; the shell owns the prompt.
        return "unknown" if self._resolve(device_key) is not None else "unavailable"

    def open(self, kind: str, device_key: str) -> AudioSource:
        resolved = self._resolve(device_key)
        if resolved is None:
            raise SourceUnavailable(device_key)
        idx, info = resolved
        return SoundDeviceSource(kind, device_key, self.sd, idx, info)


# ------------------------------------------------------------------ WASAPI loopback (system audio)


class LoopbackSource(_CallbackSource):
    def __init__(self, kind: str, device_key: str, pa_mod: Any, info: dict) -> None:
        super().__init__(kind, device_key)
        self._pa_mod = pa_mod
        self._info = info
        self.sample_rate = int(info.get("defaultSampleRate") or 48_000)
        self.channels = max(1, int(info.get("maxInputChannels") or 2))
        self._pa: Any = None
        self._stream: Any = None

    async def start(self) -> None:
        self._prepare_loop()
        pa_mod = self._pa_mod

        def cb(in_data: bytes, frame_count: int, _time_info: Any, status: int) -> tuple[None, int]:
            arr = np.frombuffer(in_data, dtype=np.float32).reshape(-1, self.channels).copy()
            self._emit(arr, bool(status & getattr(pa_mod, "paInputOverflow", 2)))
            return None, pa_mod.paContinue

        try:
            self._pa = pa_mod.PyAudio()
            self._stream = self._pa.open(
                format=pa_mod.paFloat32, channels=self.channels, rate=self.sample_rate, input=True,
                input_device_index=int(self._info["index"]), frames_per_buffer=int(self.sample_rate * _BLOCK_S),
                stream_callback=cb,
            )
            self._stream.start_stream()
        except Exception as exc:  # noqa: BLE001
            if _permission_error(exc):
                raise PermissionDenied(self.device_key) from exc
            raise SourceUnavailable(self.device_key) from exc

    def _stream_alive(self) -> bool:
        return bool(self._stream is not None and self._stream.is_active())

    async def stop(self) -> None:
        self._stopped = True
        try:
            if self._stream is not None:
                self._stream.stop_stream()
                self._stream.close()
            if self._pa is not None:
                self._pa.terminate()
        except Exception:  # noqa: BLE001, S110
            pass


class WasapiLoopbackBackend(AudioBackend):
    name = "wasapi-loopback"
    prefix = "wasapi-loopback:"

    def __init__(self) -> None:
        import pyaudiowpatch as pa_mod  # optional dependency, Windows only

        self.pa_mod = pa_mod

    def handles(self, device_key: str) -> bool:
        return device_key.startswith(self.prefix)

    def _loopbacks(self) -> list[dict]:
        p = self.pa_mod.PyAudio()
        try:
            return [dict(d) for d in p.get_loopback_device_info_generator()]
        finally:
            p.terminate()

    def list_devices(self) -> list[DeviceInfo]:
        return [
            DeviceInfo(key=f"{self.prefix}{d['name']}", name=str(d["name"]), kind="system",
                       defaultSampleRate=int(d.get("defaultSampleRate") or 0) or None,
                       channels=int(d.get("maxInputChannels") or 2), available=True,
                       note="スピーカー出力を取り込みます。マイクと重複して取り込まれる場合があります。")
            for d in self._loopbacks()
        ]

    def _resolve(self, device_key: str) -> dict | None:
        name = device_key[len(self.prefix):]
        for d in self._loopbacks():
            if d.get("name") == name:
                return d
        return None

    def probe(self, kind: str, device_key: str) -> str:
        return "granted" if self._resolve(device_key) is not None else "unavailable"

    def open(self, kind: str, device_key: str) -> AudioSource:
        info = self._resolve(device_key)
        if info is None:
            raise SourceUnavailable(device_key)
        return LoopbackSource(kind, device_key, self.pa_mod, info)


class UnsupportedSystemAudioBackend(AudioBackend):
    """Advertises that system audio is not available on this platform/installation."""

    name = "system-unsupported"

    def __init__(self, note: str) -> None:
        self.note = note

    def handles(self, device_key: str) -> bool:
        return device_key.startswith("system:") or device_key.startswith("wasapi-loopback:")

    def list_devices(self) -> list[DeviceInfo]:
        return [DeviceInfo(key="system:default", name="システム音声", kind="system", defaultSampleRate=None,
                           channels=None, available=False, note=self.note)]

    def probe(self, kind: str, device_key: str) -> str:
        return "unavailable"

    def open(self, kind: str, device_key: str) -> AudioSource:
        raise SourceUnavailable(device_key)


def live_backends() -> tuple[list[AudioBackend], dict[str, str]]:
    """Construct whichever live backends are importable. Returns (backends, status notes)."""
    backends: list[AudioBackend] = []
    notes: dict[str, str] = {}
    try:
        backends.append(SoundDeviceBackend())
        notes["microphone"] = "sounddevice"
    except Exception as exc:  # noqa: BLE001 - ImportError or PortAudio load failure
        notes["microphone"] = f"unavailable ({type(exc).__name__})"
    system_ok = False
    if sys.platform == "win32":
        try:
            backends.append(WasapiLoopbackBackend())
            notes["system"] = "wasapi-loopback"
            system_ok = True
        except Exception as exc:  # noqa: BLE001
            notes["system"] = f"unavailable ({type(exc).__name__})"
    else:
        notes["system"] = "unsupported on this OS in the MVP"
    if not system_ok:
        backends.append(UnsupportedSystemAudioBackend(
            "このOS／インストールではシステム音声の取得に対応していません。マイクのみで会議を開始できます。"))
    return backends, notes
