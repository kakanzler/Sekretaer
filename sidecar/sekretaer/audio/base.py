"""Audio source adapter interfaces (spec §4). Every capture path (microphone, OS system audio, file,
synthetic) implements :class:`AudioSource`; device discovery and permission probing go through
:class:`AudioBackend`. Adapters never bypass OS permission prompts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass

import numpy as np

PERMISSION_STATES = ("granted", "denied", "unknown", "unavailable")


class AudioSourceError(Exception):
    code = "source_unavailable"


class SourceUnavailable(AudioSourceError):
    code = "source_unavailable"


class PermissionDenied(AudioSourceError):
    code = "permission_denied"


class DeviceDisconnected(AudioSourceError):
    code = "source_unavailable"


@dataclass
class AudioFrame:
    """A block of source-native audio.

    ``data`` is float32 of shape (n, channels) in [-1, 1]. ``start_sample`` is the index of the first
    sample on the source clock; a jump relative to the previous block means samples were lost.
    ``status`` carries driver flags such as ``input_overflow``.
    """

    data: np.ndarray
    start_sample: int | None = None
    status: str | None = None


@dataclass
class DeviceInfo:
    key: str
    name: str
    kind: str  # microphone | system
    defaultSampleRate: int | None
    channels: int | None
    available: bool
    note: str | None = None

    def to_api(self) -> dict:
        return asdict(self)


class AudioSource(ABC):
    kind: str
    device_key: str
    sample_rate: int
    channels: int

    @abstractmethod
    async def start(self) -> None:
        """Open the stream. Raises PermissionDenied / SourceUnavailable."""

    @abstractmethod
    async def read(self) -> AudioFrame | None:
        """Next block, or None at end of stream. Raises DeviceDisconnected on device loss."""

    @abstractmethod
    async def stop(self) -> None: ...


class AudioBackend(ABC):
    name: str = "backend"

    @abstractmethod
    def list_devices(self) -> list[DeviceInfo]: ...

    @abstractmethod
    def handles(self, device_key: str) -> bool: ...

    @abstractmethod
    def probe(self, kind: str, device_key: str) -> str:
        """Return a permission state: granted | denied | unknown | unavailable."""

    @abstractmethod
    def open(self, kind: str, device_key: str) -> AudioSource: ...


class CompositeBackend(AudioBackend):
    name = "composite"

    def __init__(self, backends: list[AudioBackend]) -> None:
        self.backends = backends

    def list_devices(self) -> list[DeviceInfo]:
        out: list[DeviceInfo] = []
        for b in self.backends:
            try:
                out.extend(b.list_devices())
            except Exception:  # noqa: BLE001, S112 - a broken driver must not break enumeration of others
                continue
        return out

    def _pick(self, device_key: str) -> AudioBackend | None:
        for b in self.backends:
            if b.handles(device_key):
                return b
        return None

    def handles(self, device_key: str) -> bool:
        return self._pick(device_key) is not None

    def probe(self, kind: str, device_key: str) -> str:
        b = self._pick(device_key)
        if b is None:
            return "unavailable"
        return b.probe(kind, device_key)

    def open(self, kind: str, device_key: str) -> AudioSource:
        b = self._pick(device_key)
        if b is None:
            raise SourceUnavailable(device_key)
        return b.open(kind, device_key)
