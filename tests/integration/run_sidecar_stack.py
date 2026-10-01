"""Launch the real sidecar server (handshake, auth, HTTP, WebSocket) with test doubles injected.

Used by the UI ⇄ sidecar integration test (apps/web/src/lib/sidecar.integration.test.ts).
Only the engines that need hardware, models or network are replaced:

- audio: a synthetic microphone device ``synthetic:it-mic`` (speech bursts + silence, faster than realtime)
- STT: ``ScriptedStt`` (deterministic Japanese sentences)
- summarizer: ``sidecar/tests/fake_claude.py`` (never calls a model)

Run:  uv run --project sidecar python tests/integration/run_sidecar_stack.py --data-dir <dir>
Prints the normal ready line on stdout and exits on stdin EOF, like the production sidecar.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SIDECAR_TESTS = REPO / "sidecar" / "tests"
sys.path.insert(0, str(REPO / "sidecar"))

from sekretaer import __main__ as sidecar_main  # noqa: E402
from sekretaer import app_context  # noqa: E402
from sekretaer.audio.synthetic import ArraySourceSpec, SyntheticBackend, meeting_pattern  # noqa: E402
from sekretaer.config import AppConfig  # noqa: E402
from sekretaer.logsetup import setup_logging  # noqa: E402
from sekretaer.pipeline.session import SessionTuning  # noqa: E402
from sekretaer.privacy.crypto import MemorySecretStore  # noqa: E402
from sekretaer.stt.fake import ScriptedStt  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", required=True, type=Path)
    p.add_argument("--speed", type=float, default=4.0)
    args = p.parse_args()
    setup_logging("WARNING")

    control = args.data_dir / "fake-claude-control.json"
    args.data_dir.mkdir(parents=True, exist_ok=True)
    control.write_text(json.dumps({"mode": "ok"}), encoding="utf-8")

    pattern: list[tuple[str, float]] = []
    for _ in range(6):
        pattern += [("speech", 5.0), ("silence", 2.0)]
    audio = meeting_pattern(pattern)
    backend = SyntheticBackend()
    backend.add("synthetic:it-mic", lambda: ArraySourceSpec(samples=audio, speed=args.speed, hold_open=True))

    real_ctx = app_context.AppContext

    def ctx_with_doubles(config: AppConfig) -> app_context.AppContext:
        return real_ctx(
            config, backend=backend, stt_engine=ScriptedStt(), secret_store=MemorySecretStore(),
            cli_path=sys.executable, cli_prefix_args=[str(SIDECAR_TESTS / "fake_claude.py"), "--control", str(control)],
            prefer_silero=False, tuning=SessionTuning(reconnect_delay_s=0.05),
        )

    app_context.AppContext = ctx_with_doubles  # serve() resolves AppContext at call time
    config = AppConfig(data_dir=args.data_dir, allowed_origins=[], dev=False, synthetic_devices=False, stdin_watch=True)
    return asyncio.run(sidecar_main.serve(config))


if __name__ == "__main__":
    sys.exit(main())
