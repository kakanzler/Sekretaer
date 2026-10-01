"""Global (non-secret) settings persisted in the ``settings`` table. Secrets never go here."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .storage.repo import Repo

SETTINGS_KEY = "app"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SttSettings(_Strict):
    model: str = "small"
    computeType: str = "int8"
    device: Literal["cpu", "cuda", "auto"] = "cpu"


class VadSettings(_Strict):
    sensitivity: Literal["low", "normal", "high"] = "normal"


class SummarizerSettings(_Strict):
    enabled: bool = True
    cliPath: str | None = None
    timeoutSec: int = Field(default=120, ge=10, le=900)
    model: str | None = None
    maxInputChars: int = Field(default=24_000, ge=2_000, le=200_000)

    @field_validator("cliPath")
    @classmethod
    def _abs_path(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        p = Path(v)
        if not p.is_absolute():
            raise ValueError("cliPath must be an absolute path")
        if not p.is_file():
            raise ValueError("cliPath does not point to an existing file")
        return str(p)

    @field_validator("model")
    @classmethod
    def _model_name(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not all(c.isalnum() or c in "-_.[]:" for c in v) or len(v) > 100:
            raise ValueError("invalid model name")
        return v


class PrivacySettings(_Strict):
    defaultRetainAudio: bool = False
    defaultAudioRetentionDays: int = Field(default=7, ge=0, le=365)


class Settings(_Strict):
    stt: SttSettings = SttSettings()
    vad: VadSettings = VadSettings()
    summarizer: SummarizerSettings = SummarizerSettings()
    privacy: PrivacySettings = PrivacySettings()


class SettingsPatch(_Strict):
    stt: SttSettings | None = None
    vad: VadSettings | None = None
    summarizer: SummarizerSettings | None = None
    privacy: PrivacySettings | None = None


class SettingsStore:
    def __init__(self, repo: Repo) -> None:
        self.repo = repo
        raw = repo.get_setting(SETTINGS_KEY) or {}
        try:
            self.current = Settings.model_validate(raw)
        except Exception:
            # A stored cliPath may no longer exist; keep the rest.
            if isinstance(raw.get("summarizer"), dict):
                raw["summarizer"].pop("cliPath", None)
            self.current = Settings.model_validate(raw)
        env_model = os.environ.get("SEKRETAER_STT_MODEL")
        if env_model and not raw.get("stt"):
            self.current.stt.model = env_model

    def get(self) -> Settings:
        return self.current

    def update(self, patch: SettingsPatch) -> Settings:
        data = self.current.model_dump()
        for section in ("stt", "vad", "summarizer", "privacy"):
            value = getattr(patch, section)
            if value is not None:
                data[section].update(value.model_dump(exclude_unset=True))
        self.current = Settings.model_validate(data)
        self.repo.put_setting(SETTINGS_KEY, self.current.model_dump())
        return self.current
