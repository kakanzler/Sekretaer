"""Launch handshake as the Tauri shell uses it: one ready line on stdout, loopback-only listener, token
required, exit on stdin EOF (parent died). Runs the real ``python -m sekretaer`` process."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time

import httpx

from .conftest import REPO


def _reader(stream, sink: list[bytes]) -> None:
    for line in iter(stream.readline, b""):
        sink.append(line)


def test_process_handshake_auth_and_stdin_eof(tmp_path) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-m", "sekretaer", "--data-dir", str(tmp_path / "data")],
        cwd=REPO / "sidecar", stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    stderr: list[bytes] = []
    threading.Thread(target=_reader, args=(proc.stderr, stderr), daemon=True).start()
    try:
        first: list[bytes] = []
        t = threading.Thread(target=lambda: first.append(proc.stdout.readline()), daemon=True)
        t.start()
        t.join(60)
        assert first and first[0], b"".join(stderr)[-2000:]
        ready = json.loads(first[0])
        assert set(ready) == {"type", "apiVersion", "port", "token", "pid"}
        assert ready["type"] == "ready" and ready["apiVersion"] == "1"
        # On Windows the venv python.exe is a launcher; the reported pid is the interpreter's own pid.
        assert isinstance(ready["pid"], int) and ready["pid"] > 0
        assert len(ready["token"]) >= 43
        base = f"http://127.0.0.1:{ready['port']}/api/v1"
        assert httpx.get(f"{base}/health", timeout=10).status_code == 401
        r = httpx.get(f"{base}/health", headers={"Authorization": f"Bearer {ready['token']}"}, timeout=10)
        assert r.status_code == 200 and r.json()["data"]["apiVersion"] == "1"
        # Not reachable on a non-loopback address of this machine.
        try:
            lan_ip = socket.gethostbyname(socket.gethostname())
        except OSError:
            lan_ip = "127.0.0.1"
        if not lan_ip.startswith("127."):
            s = socket.socket()
            s.settimeout(2)
            assert s.connect_ex((lan_ip, ready["port"])) != 0
            s.close()
        proc.stdin.close()  # parent "dies"
        code = proc.wait(timeout=30)
        assert code == 0
        assert proc.stdout.read() == b""  # stdout carried exactly one line
        time.sleep(0.2)
        logs = b"".join(stderr).decode("utf-8", "replace")
        assert ready["token"] not in logs
        for line in logs.splitlines():
            if line.startswith("{"):
                json.loads(line)  # structured JSON logs
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)
