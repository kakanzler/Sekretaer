# Sekretär sidecar

Python sidecar for Sekretär (spec §3, §15). See `contracts/api-v1.md` for the API.

```
uv venv --python 3.12
uv sync --extra dev
uv run pytest -q
uv run python -m sekretaer --data-dir <dir> [--allowed-origin <origin>]... [--dev]
```

Optional extras: `audio` (live capture), `stt` (faster-whisper), `vad` (onnxruntime for Silero), `crypto` (encrypted audio archive).
