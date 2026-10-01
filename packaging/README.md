# Packaging (planned; not implemented yet)

Everything below is a **future step**. Today only the desktop shell builds
(`apps/desktop`). A `tauri build` works, but the result has no sidecar binary and
reports the sidecar as `failed` ("bundled sidecar executable not found").

## 1. Sidecar binary (`sekretaer-sidecar`)

The release shell starts `sekretaer-sidecar[.exe]` from the directory of the app
executable (see `apps/desktop/src-tauri/src/sidecar/launch.rs`). It must accept
the same CLI as `python -m sekretaer` (`--data-dir`, repeated
`--allowed-origin`, optional `--dev`) and follow the launch handshake in
`contracts/api-v1.md`.

Plan:

1. Build a self-contained binary from `sidecar/` with PyInstaller (one-dir
   preferred over one-file, which unpacks to a temp dir on every start and slows
   startup), per target: `x86_64-pc-windows-msvc`, `aarch64-apple-darwin`,
   `x86_64-apple-darwin`.
2. Place it at `apps/desktop/src-tauri/binaries/sekretaer-sidecar-<target-triple>[.exe]`
   (gitignored), which is the Tauri `externalBin` naming convention.
3. Only then add `"externalBin": ["binaries/sekretaer-sidecar"]` to
   `bundle` in `tauri.conf.json`. It is deliberately absent now: with it,
   `tauri build` fails without the binary. For a one-dir build, ship the
   support directory via `bundle.resources` and point `externalBin` at the
   launcher.
4. Add a script under `scripts/` that builds the sidecar for the host triple
   (`rustc --print host-tuple`) and copies it into place, and wire it into CI
   before `tauri build`.
5. Smoke test: start the packaged app with no `SEKRETAER_SIDECAR_CMD`, expect
   `sidecar://state` → `ready`, then close the window and check the sidecar
   process is gone.

Native dependencies to check in the frozen build: faster-whisper /
CTranslate2 shared libraries, onnxruntime (Silero VAD), the audio capture
backend (WASAPI loopback on Windows; system audio on macOS), and the SQLite
build.

## 2. Model manifest

STT/VAD models are **not** bundled in the installer. They are downloaded on
first use after the user has been told size, source, license and network use
(spec §5). A versioned manifest (`packaging/models.json`, to be written) will
list for each model:

- id, version, file list
- source URL (pinned revision, not `main`), expected SHA-256 per file
- license and attribution text
- size on disk, recommended device/compute type

The sidecar verifies hashes after download and refuses mismatches. Manifest
updates go through code review like code.

## 3. Signing and platform requirements

- **Windows**: Authenticode-sign the app executable, `sekretaer-sidecar.exe`
  and the MSI/NSIS installers (`bundle.windows.certificateThumbprint` or a
  custom `signCommand`), with timestamping. Sign the sidecar before Tauri
  bundles it.
- **macOS**: Developer ID signing with the hardened runtime for the app *and*
  the sidecar, then notarization and stapling. The app's `Info.plist` needs
  `NSMicrophoneUsageDescription` (and the audio-input entitlement
  `com.apple.security.device.audio-input`), because TCC attributes the
  sidecar's microphone use to the app. System-audio capture needs its own
  permission flow, still to be designed.
- Updater (if any) must verify signatures; not in the MVP.

## 4. Supply-chain record (spec §11)

For every release, record and archive with the artifacts:

- Rust: `Cargo.lock` plus an SBOM (e.g. `cargo cyclonedx`), `cargo audit` result.
- Web: `pnpm-lock.yaml` plus an SBOM, `pnpm audit` result.
- Sidecar: the locked Python dependency set (`uv.lock`), an SBOM, `pip-audit`
  result, the PyInstaller version.
- Models: the manifest above (source, hash, license).
- Tauri CLI / runtime versions and the WebView2 bootstrapper mode.
- SHA-256 of every shipped artifact and the signing certificate fingerprints.

Before the first release, write down the policy for dependency updates,
signing-key custody and rotation, and vulnerability response (who triages, how
fast, how fixes are shipped).
