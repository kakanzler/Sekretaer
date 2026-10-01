# @sekretaer/desktop — Tauri 2 shell

The desktop shell (spec §3): owns the window, starts and supervises the Python
sidecar, and hands the sidecar's connection info to the UI. The UI itself is the
statically exported Next.js app in `apps/web` (`apps/web/out`), and the sidecar
lives in `sidecar/`.

```
apps/desktop/
  package.json            # @sekretaer/desktop, Tauri CLI scripts
  src-tauri/
    tauri.conf.json       # window, CSP, frontendDist/devUrl, bundle
    capabilities/main.json# the only capability (main window)
    build.rs              # declares the app commands -> generated permissions
    src/lib.rs            # app setup, exit hook
    src/commands.rs       # the complete IPC surface
    src/sidecar.rs        # supervisor (+ sidecar/{handshake,launch,backoff}.rs)
    src/privacy.rs        # fixed OS privacy-settings URIs
    tests/supervision.rs  # smoke tests against tests/fixtures/fake_sidecar.py
```

## Prerequisites

- Rust stable (>= 1.90), plus the platform requirements for Tauri 2 (WebView2 on Windows).
- Node 24 and pnpm (workspace root), `uv` for the sidecar.
- `pnpm install` at the repo root installs `@tauri-apps/cli` for this package.

## Development

`tauri dev` runs `pnpm --filter @sekretaer/web dev` from the repo root (the
`beforeDevCommand`, which starts Next.js on `http://localhost:3000`), builds the
shell in debug mode, and opens the window on the dev server.

```sh
# from the repo root
pnpm --filter @sekretaer/desktop tauri dev
```

In debug builds the shell starts the sidecar with

```
uv run --project <repo>/sidecar python -m sekretaer \
  --data-dir <app data dir>/data \
  --allowed-origin tauri://localhost --allowed-origin http://tauri.localhost \
  --allowed-origin https://tauri.localhost --dev
```

so `uv` must be on `PATH`. The first start may take a while because `uv` syncs
the environment; the handshake timeout is 60 s.

`<app data dir>` is Tauri's app data directory for `app.sekretaer.desktop`
(Windows: `%APPDATA%\app.sekretaer.desktop`, macOS:
`~/Library/Application Support/app.sekretaer.desktop`).

### Overriding the sidecar command

Set `SEKRETAER_SIDECAR_CMD` to a **JSON array** of argv strings (never parsed by
a shell). The shell still appends `--data-dir`, the `--allowed-origin`s and, in
debug builds, `--dev`:

```powershell
$env:SEKRETAER_SIDECAR_CMD = '["C:/path/to/python.exe", "-m", "sekretaer"]'
pnpm --filter @sekretaer/desktop tauri dev
```

The override is re-read on every (re)start. It is honoured in release builds as
well (a warning is logged).

### Release builds

`pnpm --filter @sekretaer/desktop tauri build` runs `pnpm --filter
@sekretaer/web build` and embeds `apps/web/out`. Release builds look for a
bundled `sekretaer-sidecar[.exe]` next to the app executable; producing that
binary is a later packaging step (see `packaging/README.md`). Until then a
release build reports the sidecar state `failed` with a message naming the
missing path.

### Logs

The shell logs to stderr via `env_logger` (`RUST_LOG`, default `info`).
Sidecar stderr lines are forwarded under the target `sekretaer::sidecar` with
the bearer token replaced by `[REDACTED]`; lines seen before the token is known
are buffered until the handshake resolves. The token is never logged.

## IPC contract for the UI

The webview can call exactly three commands and listen to events:

| Command | Args | Result |
| --- | --- | --- |
| `sidecar_connection` | — | `{state, port, token, error}` |
| `sidecar_restart` | — | `null`; stops the current sidecar (graceful) and starts a new one, also leaves `failed` |
| `open_privacy_settings` | `{kind: "microphone"}` | `null`; opens the fixed OS page (Windows `ms-settings:privacy-microphone`, macOS Security & Privacy > Microphone). Other kinds are rejected. |

Event `sidecar://state` carries the same object on every state change.

- `state`: `"starting" | "ready" | "crashed" | "restarting" | "failed"`
- `port` / `token`: numbers/strings only when `ready`, otherwise `null`
- `error`: string for `crashed` / `failed`, otherwise `null`

All four keys are always present. Every (re)start produces a new port and token,
so the UI must reconnect (REST + WebSocket) whenever it sees a new `ready`.
Subscribe to the event first, then call `sidecar_connection` to get the current
state.

Restart policy: on unexpected exit the shell reports `crashed`, waits 1 s, 2 s,
4 s, 8 s … (max 30 s) and reports `restarting`; after 5 consecutive failures of
runs shorter than 60 s it reports `failed` and waits for `sidecar_restart`.
Launch configuration errors (invalid override, missing bundled binary) go to
`failed` immediately.

On app exit (last window closed) the shell sends `POST /api/v1/shutdown`
(1 s budget), closes the sidecar's stdin, waits up to 3 s, then kills it. If the
shell itself dies, the sidecar sees stdin EOF and exits.

## Security posture (spec §11)

- CSP (`tauri.conf.json` `app.security.csp`): `default-src 'self'`; network only
  to `ipc:`/`http://ipc.localhost` and loopback `127.0.0.1:*` (http + ws);
  `script-src 'self'` (Tauri adds hashes for the inline scripts of the static
  export at compile time); `style-src 'self' 'unsafe-inline'`. Tauri's
  automatic nonce/hash injection is disabled for `style-src` only
  (`dangerousDisableAssetCspModification: ["style-src"]`), because a nonce or
  hash in a directive makes browsers ignore `'unsafe-inline'`, which would break
  inline styles. No remote origins; `withGlobalTauri` is false.
- One capability (`capabilities/main.json`, enabled explicitly via
  `app.security.capabilities`): `core:event:allow-listen`,
  `core:event:allow-unlisten` and the three app commands. No shell, fs, opener or
  wildcard permissions; no Tauri plugins are linked. Commands are declared in
  `build.rs`, so any command not granted is denied.
- The sidecar is spawned with `tokio::process` from an argv array (no shell),
  with a hidden console on Windows and `kill_on_drop`.

If the UI needs more native API (window controls, dialogs, …) add the narrowest
specific permission to `capabilities/main.json`, never a `*:default` of a
plugin with side effects.

## Tests

```sh
cd apps/desktop/src-tauri
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test
```

`tests/supervision.rs` drives the real supervisor against
`tests/fixtures/fake_sidecar.py` (handshake, graceful shutdown via the HTTP
endpoint, crash/backoff/give-up, handshake timeout, malformed ready line, kill on
drop, token redaction in logs). It needs a Python 3 interpreter: set
`SEKRETAER_TEST_PYTHON`, or it tries `python`, `python3`, `py`, then
`uv python find`; without one those tests print `skipping` and pass.

## Icons

`src-tauri/icons/` holds generated placeholder icons. Regenerate from a
1024×1024 PNG with `pnpm --filter @sekretaer/desktop tauri icon <png>`.
