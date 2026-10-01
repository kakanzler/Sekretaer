"""Process-level configuration (from the command line) and data directory layout."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ALLOWED_ORIGINS = ("tauri://localhost", "http://tauri.localhost", "https://tauri.localhost")
DEV_ORIGIN = "http://localhost:3000"


@dataclass
class AppConfig:
    data_dir: Path
    allowed_origins: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_ORIGINS))
    dev: bool = False
    synthetic_devices: bool = False
    stdin_watch: bool = True
    host: str = "127.0.0.1"

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir).resolve()
        origins = list(dict.fromkeys([*DEFAULT_ALLOWED_ORIGINS, *self.allowed_origins]))
        if self.dev and DEV_ORIGIN not in origins:
            origins.append(DEV_ORIGIN)
        self.allowed_origins = origins
        # AC-08: never bind anything except loopback.
        self.host = "127.0.0.1"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "sekretaer.db"

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.audio_dir, self.tmp_dir, self.exports_dir, self.models_dir):
            p.mkdir(parents=True, exist_ok=True)

    def meeting_audio_dir(self, meeting_id: str) -> Path:
        return self.audio_dir / meeting_id

    def meeting_tmp_dir(self, meeting_id: str) -> Path:
        return self.tmp_dir / meeting_id

    def meeting_exports_dir(self, meeting_id: str) -> Path:
        return self.exports_dir / meeting_id
