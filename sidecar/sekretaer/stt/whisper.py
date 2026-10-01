"""faster-whisper adapter and model manager.

Models are never downloaded implicitly: import, startup and tests only look at the local model
directory. ``download()`` runs only from the explicit ``POST /stt/model/download`` action, and
``/health`` exposes name, approximate size, source and target directory beforehand (spec §5).
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import numpy as np

from ..logsetup import log
from .base import SttEngine, SttSegment, SttUnavailable

logger = logging.getLogger("sekretaer.stt")

# Approximate download sizes (CTranslate2 conversions published by Systran, MIT licence).
MODEL_CATALOG: dict[str, dict[str, Any]] = {
    "tiny": {"repo": "Systran/faster-whisper-tiny", "approxSizeMb": 75},
    "base": {"repo": "Systran/faster-whisper-base", "approxSizeMb": 145},
    "small": {"repo": "Systran/faster-whisper-small", "approxSizeMb": 484},
    "medium": {"repo": "Systran/faster-whisper-medium", "approxSizeMb": 1530},
    "large-v3": {"repo": "Systran/faster-whisper-large-v3", "approxSizeMb": 3090},
}
LICENSE = "MIT (Systran/faster-whisper model conversions of OpenAI Whisper)"


def faster_whisper_installed() -> bool:
    import importlib.util

    return importlib.util.find_spec("faster_whisper") is not None


class ModelManager:
    def __init__(self, models_dir: Path) -> None:
        self.models_dir = models_dir
        self._lock = threading.Lock()
        self.downloading: str | None = None
        self.download_error: str | None = None

    def target_dir(self, model: str) -> Path:
        return self.models_dir / f"faster-whisper-{model}"

    def is_downloaded(self, model: str) -> bool:
        d = self.target_dir(model)
        return (d / "model.bin").is_file() and (d / "config.json").is_file()

    def describe(self, model: str) -> dict[str, Any]:
        entry = MODEL_CATALOG.get(model, {})
        return {
            "model": model,
            "approxSizeMb": entry.get("approxSizeMb"),
            "source": f"https://huggingface.co/{entry['repo']}" if entry else None,
            "license": LICENSE if entry else None,
            "targetDir": str(self.target_dir(model)),
            "downloaded": self.is_downloaded(model),
            "downloading": self.downloading == model,
            "downloadError": self.download_error,
            "network": "Hugging Face Hub から HTTPS でダウンロードします（初回のみ）。",
        }

    def download(self, model: str) -> None:
        """Blocking download; call from a worker thread after an explicit user action."""
        entry = MODEL_CATALOG.get(model)
        if entry is None:
            raise SttUnavailable(f"unknown model {model}")
        with self._lock:
            self.downloading = model
            self.download_error = None
        try:
            from huggingface_hub import snapshot_download  # dependency of faster-whisper

            snapshot_download(repo_id=entry["repo"], local_dir=str(self.target_dir(model)))
        except Exception as exc:
            self.download_error = type(exc).__name__
            raise
        finally:
            self.downloading = None


class FasterWhisperStt(SttEngine):
    name = "faster-whisper"

    def __init__(self, manager: ModelManager, model: str, device: str, compute_type: str) -> None:
        self.manager = manager
        self.model = model
        self.device = device
        self.compute_type = compute_type
        self._model: Any = None
        self._state = "not_downloaded"
        self._lock = threading.Lock()
        self.refresh_state()

    def refresh_state(self) -> None:
        if self._model is not None:
            self._state = "ready"
        elif self._state not in ("loading", "failed"):
            self._state = "not_downloaded" if not self.manager.is_downloaded(self.model) else "not_loaded"

    @property
    def state(self) -> str:
        # "not_loaded" is internal: the model is on disk and will load on first use.
        return "loading" if self._state == "not_loaded" else self._state

    def ensure_loaded(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            if not self.manager.is_downloaded(self.model):
                self._state = "not_downloaded"
                raise SttUnavailable("model not downloaded")
            self._state = "loading"
            try:
                from faster_whisper import WhisperModel

                # A local directory path: faster-whisper does not touch the network.
                self._model = WhisperModel(
                    str(self.manager.target_dir(self.model)), device=self.device, compute_type=self.compute_type
                )
                self._state = "ready"
                log(logger, logging.INFO, "stt model loaded", model=self.model, device=self.device)
            except Exception as exc:
                self._state = "failed"
                log(logger, logging.ERROR, "stt model load failed", model=self.model, reason=type(exc).__name__)
                raise SttUnavailable("model load failed") from exc

    def transcribe(self, audio: np.ndarray, language: str | None, *, partial: bool) -> list[SttSegment]:
        self.ensure_loaded()
        segments, info = self._model.transcribe(
            audio.astype(np.float32),
            language=language,
            beam_size=1 if partial else 5,
            vad_filter=False,
            condition_on_previous_text=False,
            without_timestamps=partial,
        )
        out = []
        for s in segments:
            text = (s.text or "").strip()
            if not text:
                continue
            # Standard Whisper hallucination guard for silent input.
            if s.no_speech_prob > 0.6 and s.avg_logprob < -1.0:
                continue
            out.append(SttSegment(text=text, start_s=float(s.start), end_s=float(s.end),
                                  avg_logprob=float(s.avg_logprob), no_speech_prob=float(s.no_speech_prob),
                                  language=getattr(info, "language", None)))
        return out
