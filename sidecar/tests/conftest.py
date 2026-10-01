from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from sekretaer.api.app import create_app
from sekretaer.app_context import AppContext
from sekretaer.audio.synthetic import ArraySourceSpec, SyntheticBackend, meeting_pattern
from sekretaer.config import AppConfig
from sekretaer.pipeline.breakpoints import BreakpointPolicy
from sekretaer.pipeline.session import SessionTuning
from sekretaer.privacy.crypto import MemorySecretStore
from sekretaer.stt.fake import ScriptedStt

TESTS = Path(__file__).parent
REPO = TESTS.parent.parent
FAKE_CLI = TESTS / "fake_claude.py"
BASE_URL = "http://127.0.0.1"
ORIGIN = "tauri://localhost"


class FakeCli:
    def __init__(self, directory: Path) -> None:
        self.control = directory / "fake-claude-control.json"
        self.set(mode="ok")

    def set(self, **control: Any) -> None:
        self.control.write_text(json.dumps(control), encoding="utf-8")

    @property
    def prefix(self) -> list[str]:
        return [str(FAKE_CLI), "--control", str(self.control)]

    def calls(self) -> list[dict[str, Any]]:
        log = Path(str(self.control) + ".log")
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def wait_for(cond: Callable[[], Any], timeout: float = 15.0, interval: float = 0.05, msg: str = "condition") -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = cond()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {msg}")


def speech_audio(pattern: list[tuple[str, float]]) -> np.ndarray:
    return meeting_pattern(pattern)


class Harness:
    def __init__(self, tmp: Path, *, backend: SyntheticBackend | None = None, stt: ScriptedStt | None = None,
                 cli_path: str | None = None, secret_store: MemorySecretStore | None = None,
                 policy: BreakpointPolicy | None = None, tuning: SessionTuning | None = None) -> None:
        self.tmp = tmp
        tmp.mkdir(parents=True, exist_ok=True)
        self.fake_cli = FakeCli(tmp)
        self.backend = backend or SyntheticBackend()
        self.stt = stt or ScriptedStt()
        self.secrets = secret_store or MemorySecretStore()
        self.config = AppConfig(data_dir=tmp / "data", allowed_origins=[])
        self.ctx = AppContext(
            self.config, backend=self.backend, stt_engine=self.stt, secret_store=self.secrets,
            cli_path=cli_path or sys.executable, cli_prefix_args=self.fake_cli.prefix, prefer_silero=False,
            policy=policy, tuning=tuning or SessionTuning(reconnect_delay_s=0.05),
        )
        self.app = create_app(self.ctx)
        self.client = TestClient(self.app, base_url=BASE_URL)
        self.headers = {"Authorization": f"Bearer {self.ctx.token}", "Content-Type": "application/json"}

    def __enter__(self) -> Harness:
        self.client.__enter__()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.client.__exit__(*exc)

    # HTTP helpers --------------------------------------------------------
    def req(self, method: str, path: str, json_body: Any = None, headers: dict[str, str] | None = None,
            **kw: Any) -> Any:
        h = dict(self.headers)
        h.update(headers or {})
        return self.client.request(method, f"/api/v1{path}", json=json_body, headers=h, **kw)

    def ok(self, method: str, path: str, json_body: Any = None, status: int | tuple[int, ...] = (200, 201, 202),
           **kw: Any) -> Any:
        r = self.req(method, path, json_body, **kw)
        expected = status if isinstance(status, tuple) else (status,)
        assert r.status_code in expected, (r.status_code, r.text)
        return r.json()["data"]

    def add_source(self, key: str, spec_factory: Callable[[], ArraySourceSpec], kind: str = "microphone") -> None:
        self.backend.add(key, spec_factory, kind=kind)

    def create_meeting(self, *, sources: list[dict[str, str]], recording: bool = True, external: bool = True,
                       summarization: bool = True, retain: bool = False, title: str = "定例会議",
                       language: str = "ja") -> dict[str, Any]:
        return self.ok("POST", "/meetings", {
            "title": title, "language": language, "sources": sources,
            "consent": {"recording": recording, "externalProcessing": external},
            "settings": {"summarizationEnabled": summarization, "retainAudio": retain, "audioRetentionDays": 7},
        })

    def start(self, mid: str, key: str = "k1") -> Any:
        return self.req("POST", f"/meetings/{mid}/start", headers={"Idempotency-Key": key})

    def meeting(self, mid: str) -> dict[str, Any]:
        return self.ok("GET", f"/meetings/{mid}")

    def jobs(self, mid: str) -> list[dict[str, Any]]:
        return self.ok("GET", f"/meetings/{mid}/jobs")["items"]

    def wait_state(self, mid: str, state: str, timeout: float = 30.0) -> dict[str, Any]:
        return wait_for(lambda: (m := self.meeting(mid))["state"] == state and m, timeout, msg=f"state {state}")

    def events(self, cursor: int = 0) -> list[dict[str, Any]]:
        return self.ctx.bus.replay(cursor)


@pytest.fixture
def harness_factory(tmp_path: Path) -> Iterator[Callable[..., Harness]]:
    made: list[Harness] = []

    def make(**kw: Any) -> Harness:
        h = Harness(tmp_path / f"h{len(made)}", **kw)
        h.__enter__()
        made.append(h)
        return h

    yield make
    for h in reversed(made):
        h.__exit__(None, None, None)


@pytest.fixture
def harness(harness_factory: Callable[..., Harness]) -> Harness:
    return harness_factory()


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    import sekretaer.jobs.queue as q

    monkeypatch.setattr(q, "backoff_seconds", lambda attempt: 0.2)
