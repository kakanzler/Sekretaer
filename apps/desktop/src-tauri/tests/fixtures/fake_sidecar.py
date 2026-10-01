"""Fake Sekretär sidecar for the shell's supervision tests.

Usage: fake_sidecar.py <mode> <marker-file> [sidecar args...]

Implements the launch handshake of contracts/api-v1.md: one ready line on
stdout, logs on stderr, exit on stdin EOF or on an authenticated
POST /api/v1/shutdown. Progress is appended to <marker-file> so the test can
see which path was taken.

Modes:
  normal    garbage lines, ready line, then serve until shutdown / stdin EOF
  crash     ready line, then exit with code 3
  silent    never print a ready line; wait for stdin EOF
  badready  print a ready line without a token, then wait for stdin EOF
"""

import http.server
import json
import os
import secrets
import sys
import threading

mode, marker = sys.argv[1], sys.argv[2]


def mark(event):
    with open(marker, "a", encoding="utf-8") as f:
        f.write(event + "\n")


mark("pid " + str(os.getpid()))
mark("args " + json.dumps(sys.argv[3:]))

token = secrets.token_urlsafe(32)
done = threading.Event()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        ok = (
            self.path == "/api/v1/shutdown"
            and self.headers.get("Authorization") == "Bearer " + token
            and self.headers.get("Content-Type", "").startswith("application/json")
        )
        self.send_response(202 if ok else 401)
        self.send_header("Content-Length", "0")
        self.end_headers()
        if ok:
            mark("shutdown-request")
            done.set()

    def log_message(self, *args):
        pass


def wait_for_eof():
    sys.stdin.read()
    mark("eof")
    done.set()


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
port = server.server_address[1]

if mode in ("silent", "badready"):
    if mode == "badready":
        print(json.dumps({"type": "ready", "apiVersion": "1", "port": port, "pid": os.getpid()}), flush=True)
    wait_for_eof()
    os._exit(0)

print("Resolved 3 packages in 1ms", flush=True)
print(json.dumps({"type": "progress", "step": "sync"}), flush=True)
print(
    json.dumps({"type": "ready", "apiVersion": "1", "port": port, "token": token, "pid": os.getpid()}),
    flush=True,
)
sys.stderr.write(json.dumps({"level": "info", "msg": "dev connection", "token": token}) + "\n")
sys.stderr.flush()

if mode == "crash":
    os._exit(3)

threading.Thread(target=wait_for_eof, daemon=True).start()
done.wait()
mark("exit")
os._exit(0)
