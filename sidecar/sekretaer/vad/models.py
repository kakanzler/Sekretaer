"""Frame-level voice activity models on 16 kHz mono audio in 512-sample (32 ms) frames.

* :class:`SileroVad` - Silero VAD ONNX model as bundled with faster-whisper (``stt`` extra; runs on
  onnxruntime, no download). Used when importable and it passes a self-check.
* :class:`EnergyVad` - adaptive energy detector used as the fallback; reported in ``/health``.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

import numpy as np

from ..logsetup import log

FRAME = 512
SR = 16_000
logger = logging.getLogger("sekretaer.vad")

# Speech-probability thresholds per sensitivity preset (Silero recommends 0.5).
SILERO_THRESHOLDS = {"low": 0.65, "normal": 0.5, "high": 0.35}
# dB above the adaptive noise floor for the energy fallback.
ENERGY_MARGINS_DB = {"low": 14.0, "normal": 10.0, "high": 6.0}


class VadModel(ABC):
    name = "vad"
    frame = FRAME

    @abstractmethod
    def probs(self, frames: np.ndarray) -> np.ndarray:
        """frames: (n, 512) float32 -> (n,) speech probabilities in [0, 1]."""

    @abstractmethod
    def threshold(self, sensitivity: str) -> float: ...

    def reset(self) -> None:  # noqa: B027 - optional hook
        pass


class EnergyVad(VadModel):
    name = "energy"

    def __init__(self, sensitivity: str = "normal") -> None:
        self.margin = ENERGY_MARGINS_DB.get(sensitivity, 10.0)
        self.floor_db = -60.0
        self.abs_min_db = -50.0

    def reset(self) -> None:
        self.floor_db = -60.0

    def threshold(self, sensitivity: str) -> float:
        return 0.5

    def probs(self, frames: np.ndarray) -> np.ndarray:
        if frames.size == 0:
            return np.zeros(0, dtype=np.float32)
        rms = np.sqrt(np.mean(np.square(frames, dtype=np.float64), axis=1))
        db = 20.0 * np.log10(rms + 1e-10)
        out = np.empty(db.shape[0], dtype=np.float32)
        floor = self.floor_db
        for i, level in enumerate(db):
            is_speech = level > max(floor + self.margin, self.abs_min_db)
            out[i] = 1.0 if is_speech else 0.0
            # Noise floor follows quiet frames quickly and loud frames very slowly.
            if level < floor:
                floor = floor + 0.2 * (level - floor)
            elif not is_speech:
                floor = floor + 0.05 * (level - floor)
            else:
                floor = floor + 0.0005 * (level - floor)
            floor = min(max(floor, -75.0), -20.0)
        self.floor_db = floor
        return out


class SileroVad(VadModel):
    """Streaming wrapper over faster-whisper's bundled Silero model.

    The bundled model call is stateless per invocation, so each block is evaluated together with a
    short look-back window and only the new frames' probabilities are kept.
    """

    name = "silero"
    LOOKBACK_FRAMES = 32  # ~1 s of context

    def __init__(self, model: Any, two_d: bool = False) -> None:
        self.model = model
        self.two_d = two_d  # faster-whisper 1.1.x takes (1, n); 1.2+ takes (n,)
        self._history = np.zeros((0, FRAME), dtype=np.float32)

    @classmethod
    def load(cls) -> SileroVad:
        from faster_whisper.vad import get_vad_model  # optional dependency

        model = get_vad_model()
        for two_d in (False, True):
            inst = cls(model, two_d=two_d)
            try:
                test = inst.probs(np.zeros((8, FRAME), dtype=np.float32))
            except (AssertionError, ValueError):
                continue
            if test.shape == (8,) and np.all(np.isfinite(test)):
                inst.reset()
                return inst
        raise RuntimeError("unsupported faster-whisper Silero interface")

    def threshold(self, sensitivity: str) -> float:
        return SILERO_THRESHOLDS.get(sensitivity, 0.5)

    def reset(self) -> None:
        self._history = np.zeros((0, FRAME), dtype=np.float32)

    def probs(self, frames: np.ndarray) -> np.ndarray:
        if frames.size == 0:
            return np.zeros(0, dtype=np.float32)
        window = np.concatenate([self._history, frames], axis=0)
        flat = window.reshape(1, -1) if self.two_d else window.reshape(-1)
        out = np.asarray(self.model(flat.astype(np.float32)))
        out = out.reshape(-1)[-frames.shape[0]:]
        self._history = window[-self.LOOKBACK_FRAMES:]
        return out.astype(np.float32)


def create_vad(sensitivity: str, prefer_silero: bool = True) -> tuple[VadModel, dict[str, Any]]:
    """Return a VAD instance plus a health description."""
    if prefer_silero:
        try:
            model = SileroVad.load()
            return model, {"state": "ready", "engine": "silero", "sensitivity": sensitivity}
        except Exception as exc:  # noqa: BLE001 - ImportError, onnxruntime errors, missing asset
            log(logger, logging.INFO, "silero vad unavailable, using energy fallback", reason=type(exc).__name__)
    return EnergyVad(sensitivity), {
        "state": "fallback",
        "engine": "energy",
        "sensitivity": sensitivity,
        "note": "Silero VAD を読み込めないため、簡易エネルギー VAD で動作しています。",
    }
