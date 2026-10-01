"""``claude -p`` subprocess wrapper (spec §6, §11).

* executable resolved once (configured absolute path, else ``shutil.which``); never a shell
* ``asyncio.create_subprocess_exec`` with an argument list; the fixed prompt is an argument, the
  transcript/previous note go through stdin
* all built-in tools disabled (``--tools ""``), no MCP servers, no slash commands/skills, no session
  persistence; optional hardening flags are only used when ``claude --help`` lists them
* minimal environment, private empty working directory, timeout, stdout/stderr caps, kill on cancel
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..logsetup import log
from .prompt import SYSTEM_PROMPT

logger = logging.getLogger("sekretaer.cli")

STDOUT_CAP = 2 * 1024 * 1024
STDERR_CAP = 256 * 1024
PROBE_TIMEOUT_S = 20.0

# Environment variables passed through to the CLI (upper-cased comparison). Everything else is dropped.
ENV_ALLOW = {
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "HOME", "USERPROFILE", "HOMEDRIVE",
    "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "TEMP", "TMP", "TMPDIR", "USER",
    "USERNAME", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
    "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
    # The CLI's own authentication/provider selection (passed through, never read or stored by us).
    "CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION", "GOOGLE_APPLICATION_CREDENTIALS", "AWS_PROFILE",
    "AWS_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_BEARER_TOKEN_BEDROCK",
}
ENV_FORCE = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}

REQUIRED_FLAGS = ("--print", "--output-format", "--tools")
HARDENING_FLAGS = ("--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands", "--safe-mode")

_AUTH_PATTERNS = re.compile(
    r"(not logged in|please run /login|log ?in|authenticat|invalid api key|api key|unauthori[sz]ed|\b401\b|"
    r"oauth|credential|subscription)",
    re.IGNORECASE,
)
_CMD_UNSAFE = re.compile(r'[\r\n%^&|<>"!]')


class CliError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass
class CliInfo:
    state: str = "unknown"  # ready | cli_not_found | cli_auth_required | unsupported | unknown
    version: str | None = None
    path: str | None = None
    flags: set[str] = field(default_factory=set)
    detail: str | None = None


@dataclass
class CliResult:
    text: str
    envelope: dict[str, Any] | None
    duration_ms: int


def _clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() in ENV_ALLOW}
    env.update(ENV_FORCE)
    return env


def _node_shim_target(cmd_path: Path) -> list[str] | None:
    """npm installs ``claude.cmd``; run the underlying script with node instead of via cmd.exe."""
    script = cmd_path.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
    node = shutil.which("node")
    if script.is_file() and node:
        return [node, str(script)]
    return None


class ClaudeCli:
    def __init__(
        self, *, workdir: Path, cli_path: str | None = None, prefix_args: list[str] | None = None,
        timeout_s: float = 120.0, model: str | None = None,
    ) -> None:
        self.workdir = workdir
        self.cli_path = cli_path
        self.prefix_args = list(prefix_args or [])
        self.timeout_s = timeout_s
        self.model = model
        self.info = CliInfo()
        self._argv0: list[str] | None = None
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._active: set[str] = set()
        self._canceled: set[str] = set()

    # ------------------------------------------------------------------ resolution / detection
    def configure(self, *, cli_path: str | None, timeout_s: float, model: str | None) -> None:
        if cli_path != self.cli_path:
            self.cli_path = cli_path
            self._argv0 = None
            self.info = CliInfo()
        self.timeout_s = timeout_s
        self.model = model

    def resolve(self) -> list[str]:
        if self._argv0 is not None:
            return self._argv0
        if self.cli_path:
            p = Path(self.cli_path)
            if not p.is_absolute() or not p.is_file():
                raise CliError("cli_not_found", "configured path does not exist")
            found = str(p)
        else:
            found = shutil.which("claude")
            if not found:
                raise CliError("cli_not_found", "claude not on PATH")
        argv0 = [found]
        if sys.platform == "win32" and found.lower().endswith((".cmd", ".bat")):
            shim = _node_shim_target(Path(found))
            if shim is None:
                raise CliError("cli_not_found", "batch shim is not supported; configure claude.exe or cli.js")
            argv0 = shim
        self._argv0 = [*argv0, *self.prefix_args]
        self.info.path = found
        return self._argv0

    async def detect(self) -> CliInfo:
        """Run ``--version`` and ``--help`` once to learn version and supported flags."""
        try:
            argv0 = self.resolve()
        except CliError as exc:
            self.info = CliInfo(state="cli_not_found", detail=exc.detail)
            return self.info
        try:
            rc, out, _err = await self._exec_simple([*argv0, "--version"])
            m = re.search(r"\d+\.\d+\.\d+", out)
            self.info.version = m.group(0) if m else None
            rc2, help_text, _ = await self._exec_simple([*argv0, "--help"])
        except CliError as exc:
            self.info.state = exc.code
            self.info.detail = exc.detail
            return self.info
        flags = set(re.findall(r"(?<![\w-])(--[a-zA-Z][\w-]*)", help_text))
        if re.search(r"(^|\s)-p,\s*--print", help_text):
            flags.add("--print")
        self.info.flags = flags
        missing = [f for f in REQUIRED_FLAGS if f not in flags]
        if rc != 0 or rc2 != 0 or missing:
            self.info.state = "unsupported"
            self.info.detail = f"missing flags: {','.join(missing)}" if missing else "probe failed"
        else:
            self.info.state = "ready"
        log(logger, logging.INFO, "claude cli detected", state=self.info.state, version=self.info.version,
            hardening=[f for f in HARDENING_FLAGS if f in flags])
        return self.info

    async def _exec_simple(self, argv: list[str]) -> tuple[int, str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, cwd=str(self._ensure_workdir()), env=_clean_env(),
                **_spawn_kwargs(),
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise CliError("cli_not_found", type(exc).__name__) from exc
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=PROBE_TIMEOUT_S)
        except TimeoutError:
            _kill(proc)
            await _reap(proc)
            raise CliError("cli_timeout", "probe timed out") from None
        except asyncio.CancelledError:
            _kill(proc)
            await _reap(proc)
            raise
        return proc.returncode or 0, out[:STDOUT_CAP].decode("utf-8", "replace"), err[:STDERR_CAP].decode(
            "utf-8", "replace")

    def _ensure_workdir(self) -> Path:
        self.workdir.mkdir(parents=True, exist_ok=True)
        return self.workdir

    # ------------------------------------------------------------------ invocation
    def build_argv(self, user_prompt: str) -> list[str]:
        argv0 = self.resolve()
        flags = self.info.flags
        args = ["-p", "--output-format", "json", "--tools", ""]
        args += [f for f in HARDENING_FLAGS if f in flags]
        prompt = user_prompt
        if "--system-prompt" in flags or not flags:
            args += ["--system-prompt", SYSTEM_PROMPT]
        else:
            prompt = SYSTEM_PROMPT + "\n\n" + user_prompt
        if self.model:
            args += ["--model", self.model]
        # Positional prompt last: it must not follow the variadic --tools option directly.
        args.append(prompt)
        argv = [*argv0, *args]
        if sys.platform == "win32" and argv0[0].lower().endswith((".cmd", ".bat")):
            if any(_CMD_UNSAFE.search(a) for a in argv[1:]):
                raise CliError("cli_not_found", "unsafe arguments for a batch shim")
        return argv

    async def run(self, stdin_text: str, user_prompt: str, *, key: str) -> CliResult:
        self._active.add(key)
        try:
            return await self._run(stdin_text, user_prompt, key=key)
        finally:
            self._active.discard(key)

    async def _run(self, stdin_text: str, user_prompt: str, *, key: str) -> CliResult:
        if key in self._canceled:
            raise CliError("canceled")
        if self.info.state == "unknown":
            await self.detect()
        if self.info.state == "cli_not_found":
            raise CliError("cli_not_found", self.info.detail or "")
        if self.info.state == "unsupported":
            raise CliError("cli_not_found", self.info.detail or "unsupported CLI version")
        argv = self.build_argv(user_prompt)
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, cwd=str(self._ensure_workdir()), env=_clean_env(),
                **_spawn_kwargs(),
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            self.info.state = "cli_not_found"
            raise CliError("cli_not_found", type(exc).__name__) from exc
        self._procs[key] = proc
        overflow = {"flag": False}

        async def pump(stream: asyncio.StreamReader, cap: int) -> bytes:
            buf = bytearray()
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    break
                if len(buf) + len(chunk) > cap:
                    overflow["flag"] = True
                    _kill(proc)
                    break
                buf.extend(chunk)
            return bytes(buf)

        async def feed() -> None:
            assert proc.stdin is not None
            try:
                proc.stdin.write(stdin_text.encode("utf-8"))
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                try:
                    proc.stdin.close()
                except Exception:  # noqa: BLE001, S110
                    pass

        assert proc.stdout is not None and proc.stderr is not None
        try:
            _, out, err, rc = await asyncio.wait_for(
                asyncio.gather(feed(), pump(proc.stdout, STDOUT_CAP), pump(proc.stderr, STDERR_CAP), proc.wait()),
                timeout=self.timeout_s,
            )
        except TimeoutError:
            _kill(proc)
            await _reap(proc)
            raise CliError("cli_timeout") from None
        except asyncio.CancelledError:
            _kill(proc)
            await _reap(proc)
            raise
        finally:
            self._procs.pop(key, None)
        duration_ms = int((loop.time() - started) * 1000)
        if key in self._canceled:
            raise CliError("canceled")
        if overflow["flag"]:
            raise CliError("summary_invalid_schema", "output exceeded size cap")
        out_text = out.decode("utf-8", "replace")
        err_text = err.decode("utf-8", "replace")
        envelope: dict[str, Any] | None = None
        try:
            parsed = json.loads(out_text) if out_text.strip() else None
            if isinstance(parsed, dict):
                envelope = parsed
        except json.JSONDecodeError:
            envelope = None
        if envelope is not None and envelope.get("is_error"):
            detail_text = str(envelope.get("result") or envelope.get("subtype") or "")
            if _AUTH_PATTERNS.search(detail_text) or _AUTH_PATTERNS.search(err_text):
                self.info.state = "cli_auth_required"
                raise CliError("cli_auth_required")
            raise CliError("cli_failed", str(envelope.get("subtype") or "error"))
        if rc != 0:
            if _AUTH_PATTERNS.search(err_text) or _AUTH_PATTERNS.search(out_text[:4000]):
                self.info.state = "cli_auth_required"
                raise CliError("cli_auth_required")
            raise CliError("cli_failed", f"exit {rc}")
        if envelope is None:
            # --output-format json should always produce an envelope; anything else is invalid output.
            raise CliError("summary_invalid_schema", "CLI output is not a JSON envelope")
        result = envelope.get("result")
        if not isinstance(result, str):
            raise CliError("summary_invalid_schema", "missing result text")
        if self.info.state == "cli_auth_required":
            self.info.state = "ready"
        log(logger, logging.INFO, "claude cli finished", durationMs=duration_ms, outBytes=len(out),
            costUsd=envelope.get("total_cost_usd"))
        return CliResult(text=result, envelope=envelope, duration_ms=duration_ms)

    def cancel(self, key: str) -> bool:
        """Cancel ``key`` (job id): kill its process and refuse later runs until :meth:`release`.

        The mark is set even when no process is running yet, so a cancel that lands between the job
        being marked running and its first (or next chunk/repair) ``run`` still takes effect.
        Returns True if a process was running.
        """
        self._canceled.add(key)
        proc = self._procs.get(key)
        if proc is not None:
            _kill(proc)
        return key in self._active

    def release(self, key: str) -> None:
        """Forget the cancel mark for ``key`` once its job is finished (a retried job may run again)."""
        self._canceled.discard(key)

    def kill_all(self) -> None:
        for key, proc in list(self._procs.items()):
            self._canceled.add(key)
            _kill(proc)


def _spawn_kwargs() -> dict[str, Any]:
    if sys.platform == "win32":
        import subprocess

        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {"start_new_session": True}


def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if sys.platform != "win32":
            import signal

            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


async def _reap(proc: asyncio.subprocess.Process) -> None:
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except (TimeoutError, ProcessLookupError):
        pass
