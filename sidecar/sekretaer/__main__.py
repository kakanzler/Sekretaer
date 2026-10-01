"""Entry point: ``python -m sekretaer --data-dir <dir> [--allowed-origin <origin>]... [--dev]``.

Launch handshake (contracts/api-v1.md): bind 127.0.0.1 on an OS-chosen port, generate a random token,
write exactly one JSON ready line to stdout, then never use stdout again. Exit on stdin EOF (parent
died), SIGTERM/SIGINT or ``POST /api/v1/shutdown``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import sys
import threading
from pathlib import Path

from . import API_VERSION
from .config import AppConfig
from .logsetup import log, setup_logging

logger = logging.getLogger("sekretaer.main")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m sekretaer", description="Sekretär sidecar")
    p.add_argument("--data-dir", required=True, type=Path, help="application data directory")
    p.add_argument("--allowed-origin", action="append", default=[], help="additional allowed Origin (repeatable)")
    p.add_argument("--dev", action="store_true", help="browser-only development mode (allows http://localhost:3000)")
    p.add_argument("--synthetic-audio", action="store_true", help="expose synthetic demo input devices")
    p.add_argument("--no-stdin-watch", action="store_true", help="do not exit on stdin EOF (manual runs)")
    p.add_argument("--log-level", default=os.environ.get("SEKRETAER_LOG_LEVEL", "INFO"))
    return p.parse_args(argv)


def _bind_loopback() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: prevent another process from hijacking the port
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.setblocking(False)
    return sock


def _watch_stdin(loop: asyncio.AbstractEventLoop, event: asyncio.Event) -> None:
    stream = sys.stdin.buffer if sys.stdin is not None else None
    try:
        while stream is not None:
            chunk = stream.read1(4096) if hasattr(stream, "read1") else stream.read(4096)
            if not chunk:
                break
    except (OSError, ValueError):
        pass
    loop.call_soon_threadsafe(event.set)


async def serve(config: AppConfig) -> int:
    import uvicorn

    from .api.app import create_app
    from .app_context import AppContext

    ctx = AppContext(config)
    app = create_app(ctx)
    sock = _bind_loopback()
    port = sock.getsockname()[1]
    uv_config = uvicorn.Config(app, log_config=None, access_log=False, lifespan="on", ws="websockets",
                               timeout_graceful_shutdown=5, server_header=False, date_header=False)
    server = uvicorn.Server(uv_config)
    serve_task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        if serve_task.done():
            await serve_task
            return 1
        await asyncio.sleep(0.02)
    ready = {"type": "ready", "apiVersion": API_VERSION, "port": port, "token": ctx.token, "pid": os.getpid()}
    sys.stdout.write(json.dumps(ready) + "\n")
    sys.stdout.flush()
    # stdout is reserved for the handshake; anything printed later goes to stderr.
    sys.stdout = sys.stderr
    if config.dev:
        sys.stderr.write(f"[sekretaer --dev] http://127.0.0.1:{port}/api/v1  token={ctx.token}\n")
        sys.stderr.flush()
    loop = asyncio.get_running_loop()
    if config.stdin_watch:
        threading.Thread(target=_watch_stdin, args=(loop, ctx.shutdown_event), name="stdin-watch", daemon=True).start()
    log(logger, logging.INFO, "listening", port=port, origins=len(config.allowed_origins), dev=config.dev)
    shutdown_wait = asyncio.create_task(ctx.shutdown_event.wait())
    await asyncio.wait({serve_task, shutdown_wait}, return_when=asyncio.FIRST_COMPLETED)
    if not serve_task.done():
        log(logger, logging.INFO, "shutting down")
        server.should_exit = True
        await serve_task
    shutdown_wait.cancel()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.log_level.upper())
    config = AppConfig(
        data_dir=args.data_dir,
        allowed_origins=list(args.allowed_origin),
        dev=args.dev,
        synthetic_devices=args.synthetic_audio,
        stdin_watch=not args.no_stdin_watch,
    )
    try:
        return asyncio.run(serve(config))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
