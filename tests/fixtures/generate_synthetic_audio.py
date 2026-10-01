"""Generate small synthetic meeting audio fixtures (speech-like bursts, sine, noise and silence).

No binary fixtures are committed; tests generate what they need at run time. Use this script to
produce WAV files for manual runs of the sidecar with a ``file:<path>`` input device:

    cd sidecar && uv run python ../tests/fixtures/generate_synthetic_audio.py --out <dir>

Writes ``meeting-16k-mono.wav`` (about 40 s) and ``meeting-48k-stereo.wav`` (about 12 s).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sidecar"))

from sekretaer.audio.synthetic import meeting_pattern, write_wav  # noqa: E402

PATTERN_16K = [("speech", 6.0), ("silence", 0.7), ("speech", 5.0), ("silence", 0.6), ("speech", 7.0),
               ("silence", 2.5), ("speech", 6.0), ("silence", 0.8), ("speech", 8.0), ("silence", 2.0)]
PATTERN_48K = [("speech", 4.0), ("silence", 1.0), ("sine", 1.0), ("silence", 1.0), ("speech", 3.0), ("silence", 2.0)]


def write_stereo_wav(path: Path, left: np.ndarray, right: np.ndarray, sr: int) -> None:
    import wave

    pcm = (np.clip(np.stack([left, right], axis=1), -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def generate(out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    mono = out / "meeting-16k-mono.wav"
    write_wav(mono, meeting_pattern(PATTERN_16K, sr=16_000, noise_level=0.002), 16_000)
    stereo = out / "meeting-48k-stereo.wav"
    sig = meeting_pattern(PATTERN_48K, sr=48_000, noise_level=0.002)
    write_stereo_wav(stereo, sig, sig * 0.8, 48_000)
    return [mono, stereo]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    for path in generate(args.out):
        print(f"wrote {path} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
