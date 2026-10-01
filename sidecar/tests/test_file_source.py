"""A 48 kHz stereo WAV fed through the ``file:`` adapter: source format is recorded, audio is converted to
16 kHz mono for VAD/STT, and utterance timing survives the conversion."""

from __future__ import annotations

import importlib.util

from .conftest import REPO, Harness, wait_for


def _generator():
    path = REPO / "tests" / "fixtures" / "generate_synthetic_audio.py"
    spec = importlib.util.spec_from_file_location("gen_audio", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_48k_stereo_wav_is_resampled_and_segmented(harness: Harness, tmp_path) -> None:
    mono, stereo = _generator().generate(tmp_path / "fixtures")
    assert stereo.stat().st_size < 2_500_000
    m = harness.create_meeting(sources=[{"kind": "microphone", "deviceKey": f"file:{stereo}"}], external=False)
    mid = m["id"]
    assert harness.start(mid).status_code == 200
    src = harness.meeting(mid)["sources"][0]
    assert src["sampleRate"] == 48_000 and src["channels"] == 2 and src["offsetMs"] == 0
    segs = wait_for(lambda: (s := harness.ok("GET", f"/meetings/{mid}/transcript")["items"]) and len(s) >= 3 and s,
                    msg="segments from wav")
    # speech 0-4 s, sine 5-6 s, speech 7-10 s (each padded by 300 ms)
    assert abs(segs[0]["startMs"] - 0) <= 100 and abs(segs[0]["endMs"] - 4300) <= 150
    assert abs(segs[-1]["startMs"] - 6700) <= 150 and abs(segs[-1]["endMs"] - 10300) <= 150
    assert not [e for e in harness.events() if e["event"] == "audio.gap"]
