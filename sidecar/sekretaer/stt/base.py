"""Speech-to-text engine interface (spec §5)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import numpy as np

STT_STATES = ("not_downloaded", "loading", "ready", "failed", "unavailable")


@dataclass
class SttSegment:
    text: str
    start_s: float
    end_s: float
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    language: str | None = None


class SttUnavailable(Exception):
    """Model not installed / not downloaded / failed to load -> ``stt_model_unavailable``."""


class SttEngine(ABC):
    name = "stt"
    model = ""
    device = "cpu"
    compute_type = "int8"

    @property
    @abstractmethod
    def state(self) -> str: ...

    def ensure_loaded(self) -> None:  # noqa: B027 - optional hook
        """Load the model if possible (never downloads). Raises SttUnavailable."""

    @abstractmethod
    def transcribe(self, audio: np.ndarray, language: str | None, *, partial: bool) -> list[SttSegment]:
        """audio: 16 kHz mono float32. language: None for auto-detect."""

    def health(self) -> dict[str, Any]:
        return {"state": self.state, "model": self.model, "device": self.device, "computeType": self.compute_type}


class UnavailableStt(SttEngine):
    name = "none"

    def __init__(self, reason: str = "faster-whisper is not installed") -> None:
        self.reason = reason

    @property
    def state(self) -> str:
        return "unavailable"

    def ensure_loaded(self) -> None:
        raise SttUnavailable(self.reason)

    def transcribe(self, audio: np.ndarray, language: str | None, *, partial: bool) -> list[SttSegment]:
        raise SttUnavailable(self.reason)
