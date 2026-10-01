"""Regenerate contracts/openapi/openapi.json from the sidecar FastAPI app.

Usage (from the repo root or sidecar/):
    cd sidecar && uv run python ../scripts/export-openapi.py [--check]

--check exits non-zero when the committed file differs from the generated one.
The app is built against a throw-away data directory with fake components; nothing is downloaded and
no audio device or claude CLI is touched.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SIDECAR = ROOT / "sidecar"
TARGET = ROOT / "contracts" / "openapi" / "openapi.json"


def generate() -> str:
    sys.path.insert(0, str(SIDECAR))
    from sekretaer.api.app import create_app
    from sekretaer.app_context import AppContext
    from sekretaer.audio.synthetic import SyntheticBackend
    from sekretaer.config import AppConfig
    from sekretaer.privacy.crypto import MemorySecretStore
    from sekretaer.stt.base import UnavailableStt

    tmp = Path(tempfile.mkdtemp(prefix="sekretaer-openapi-"))
    try:
        ctx = AppContext(AppConfig(data_dir=tmp / "data"), backend=SyntheticBackend(), stt_engine=UnavailableStt(),
                         secret_store=MemorySecretStore(), prefer_silero=False,
                         token="export")  # noqa: S106 - placeholder; the app is never served here
        try:
            schema = create_app(ctx).openapi()
        finally:
            ctx.db.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--out", type=Path, default=TARGET)
    args = parser.parse_args()
    text = generate()
    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.exists() else ""
        if current != text:
            print(f"{args.out} is out of date; run scripts/export-openapi.py", file=sys.stderr)
            return 1
        print(f"{args.out} is up to date")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {args.out} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
