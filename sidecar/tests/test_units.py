"""Unit tests: resampler, ring buffer, VAD segmenter, breakpoint policy, draft mapping, crypto, CLI detection."""

from __future__ import annotations

import asyncio
import sys

import numpy as np
import pytest

from sekretaer.audio.resample import StreamingResampler, to_mono
from sekretaer.audio.ringbuffer import RingBuffer
from sekretaer.audio.synthetic import meeting_pattern
from sekretaer.pipeline.breakpoints import BreakpointDetector, BreakpointPolicy, completion_cue
from sekretaer.privacy.crypto import AudioCipher, MemorySecretStore
from sekretaer.summarization.cli import ClaudeCli, CliError
from sekretaer.summarization.draft import (
    MapContext,
    build_alias_table,
    draft_to_note,
    extract_json_object,
    note_to_draft,
)
from sekretaer.summarization.schema import note_errors
from sekretaer.util import new_id
from sekretaer.vad.models import EnergyVad
from sekretaer.vad.segmenter import Segmenter, SpeechEnd, SpeechPartial, SpeechStart

from .conftest import FAKE_CLI


# ------------------------------------------------------------------ audio
def test_resampler_48k_to_16k_streaming_matches_length_and_frequency() -> None:
    sr = 48_000
    t = np.arange(sr * 2) / sr
    x = np.sin(2 * np.pi * 440 * t).astype(np.float32)
    rs = StreamingResampler(sr)
    out = np.concatenate([rs.process(x[i : i + 4800]) for i in range(0, x.size, 4800)])
    assert abs(out.size - 32_000) <= 2
    spectrum = np.abs(np.fft.rfft(out[:16_000]))
    assert abs(int(np.argmax(spectrum)) - 440) <= 2
    stereo = np.stack([x[:10], -x[:10]], axis=1)
    assert np.allclose(to_mono(stereo), 0)


def test_ring_buffer_reports_overwritten_samples() -> None:
    rb = RingBuffer(1000)
    rb.write(np.ones(600, dtype=np.float32))
    start, data, lost = rb.read_available(100)
    assert start == 0 and data is not None and data.size == 600 and lost == 0
    rb.write(np.arange(1500, dtype=np.float32))  # 2100 written, only the newest 1000 retained
    start, data, lost = rb.read_available(100)
    assert lost == 500 and start == 1100 and data is not None and data.size == 1000
    assert rb.get(0, 10) is None  # already overwritten
    rb.write_silence(50)
    assert rb.write_pos == 2150


def test_segmenter_padding_end_silence_and_partials() -> None:
    seg = Segmenter(threshold=0.5)
    # 32 ms frames: 20 silent, 100 speech (3.2 s), 30 silent
    probs = np.array([0.0] * 20 + [1.0] * 100 + [0.0] * 30)
    events = seg.process(0, probs)
    starts = [e for e in events if isinstance(e, SpeechStart)]
    ends = [e for e in events if isinstance(e, SpeechEnd)]
    partials = [e for e in events if isinstance(e, SpeechPartial)]
    assert len(starts) == 1 and len(ends) == 1 and len(partials) == 2
    assert starts[0].speech_start == 20 * 512 and starts[0].start == 20 * 512 - 4800  # 300 ms pad
    assert ends[0].speech_end == 120 * 512 and ends[0].end == 120 * 512 + 4800
    # ~500 ms of silence closes the candidate: end emitted at frame 120 + 16
    assert not seg.in_speech


def test_segmenter_force_splits_long_speech() -> None:
    seg = Segmenter(threshold=0.5)
    events = seg.process(0, np.ones(1000))  # 32 s continuous speech
    ends = [e for e in events if isinstance(e, SpeechEnd)]
    assert ends and ends[0].forced and ends[0].end - ends[0].start <= 25 * 16000 + 512


def test_energy_vad_separates_speech_from_silence() -> None:
    audio = meeting_pattern([("silence", 1.0), ("speech", 1.0), ("silence", 1.0)], noise_level=0.001)
    vad = EnergyVad("normal")
    frames = audio[: audio.size // 512 * 512].reshape(-1, 512)
    p = vad.probs(frames)
    assert p[:25].mean() < 0.1 and p[35:55].mean() > 0.9 and p[70:].mean() < 0.2


# ------------------------------------------------------------------ breakpoints
def _speech(det: BreakpointDetector, start: int, end: int, text: str) -> None:
    det.on_speech_start("mic", start)
    det.on_speech_end("mic", end, end + 300)
    det.on_final(start, end + 300, text)


def test_silence_breakpoint_requires_15s_and_1_2s_silence() -> None:
    det = BreakpointDetector(BreakpointPolicy())
    _speech(det, 0, 6000, "最初の議題について説明します。背景と目的を順番に確認します。")
    assert det.evaluate(9000, None) is None  # only 6 s unsummarized
    _speech(det, 12_000, 20_000, "次に試作の進捗を共有しました。第一版は予定どおり完成しています。")
    assert det.evaluate(20_800, None) is None  # 0.8 s silence so far
    trig = det.evaluate(21_300, None)
    assert trig is not None and trig.kind == "silence" and trig.range_end_ms == 20_300


def test_breakpoint_waits_for_pending_stt() -> None:
    det = BreakpointDetector(BreakpointPolicy())
    text = "長い説明がありました。背景、課題、対応方針、スケジュールを順番に詳しく説明しました。以上です。"
    _speech(det, 0, 20_000, text)
    assert det.evaluate(22_000, stt_pending_until=20_300) is None
    assert det.evaluate(22_000, None) is not None


def test_incomplete_utterance_needs_longer_silence() -> None:
    assert completion_cue("これで決定しました。") == "complete"
    assert completion_cue("それについては、") == "incomplete"
    assert completion_cue("we agreed") == "complete"
    det = BreakpointDetector(BreakpointPolicy())
    _speech(det, 0, 18_000, "予算の件ですが、上限については経理と確認中で、来週には回答が来る見込みで、それから")
    assert det.evaluate(19_500, None) is None  # 1.5 s silence not enough after an unfinished phrase
    assert det.evaluate(20_600, None) is not None


def test_short_text_is_suppressed_but_long_speech_breaks() -> None:
    det = BreakpointDetector(BreakpointPolicy())
    _speech(det, 0, 16_000, "はい。")
    assert det.evaluate(30_000, None) is None  # too little text
    det2 = BreakpointDetector(BreakpointPolicy())
    t = 0
    for _ in range(10):  # 10 x 9.5 s with 0.5 s pauses: never 1.2 s of silence
        det2.on_speech_start("mic", t)
        det2.on_final(t, t + 9_500, "議論が続いています。" * 3)
        t += 10_000
        assert det2.evaluate(t, None) is None or t >= 90_000
    trig = det2.evaluate(t, None)
    assert trig is None or trig.kind == "long_speech"
    det3 = BreakpointDetector(BreakpointPolicy())
    for i in range(10):
        det3.on_final(i * 10_000, i * 10_000 + 9_500, "議論が続いています。" * 3)
    trig = det3.evaluate(100_000, None)
    assert trig is not None and trig.kind == "long_speech" and trig.range_end_ms == 99_500
    det3.on_job_created(trig.range_end_ms, 100_000)
    assert det3.evaluate(101_000, None) is None


def test_other_source_speaking_prevents_silence_break() -> None:
    det = BreakpointDetector(BreakpointPolicy())
    det.on_speech_start("mic", 0)
    det.on_speech_start("sys", 1000)
    det.on_final(0, 17_300, "マイク側の発言が終わりました。")
    det.on_speech_end("mic", 17_000, 17_300)
    assert det.evaluate(20_000, None) is None  # system audio still active


# ------------------------------------------------------------------ draft mapping
def _segs(n: int) -> list[dict]:
    return [{"id": new_id(), "start_ms": i * 1000, "end_ms": i * 1000 + 900, "text": f"t{i}", "source_id": "s",
             "speaker_label": None} for i in range(n)]


def test_alias_mapping_roundtrip_and_ref_stability() -> None:
    segs = _segs(3)
    table = build_alias_table(context=[], transcript=segs, previous_segments=[])
    mid, rid = new_id(), new_id()
    ctx = MapContext(meeting_id=mid, revision_id=rid, generated_at="2026-09-30T10:00:00.000+09:00",
                     coverage_from_ms=0, coverage_to_ms=2900, section_from_ms=0, section_to_ms=2900,
                     previous_note=None, table=table)
    draft = {
        "cornell": {"cues": [{"text": "cue", "evidence": ["S1"]}], "notes": [], "summary": "s",
                    "sectionSummary": "sec"},
        "bullets": [{"ref": None, "text": "b", "evidence": ["S2", "S2", "S9"], "children": []}],
        "decisions": [{"ref": None, "text": "d", "status": "agreed", "certainty": 0.5, "evidence": ["S3"]}],
        "actions": [{"ref": None, "text": "a", "assignee": " ", "dueDate": "来週", "status": "weird",
                     "evidence": []}],
        "openQuestions": [{"text": "  ", "evidence": ["S1"]}],
    }
    note = draft_to_note(draft, ctx)
    assert note_errors(note) == []
    assert note["bullets"][0]["evidenceSegmentIds"] == [segs[1]["id"]]
    assert note["decisions"][0]["status"] == "needs_review"  # low certainty cannot be "agreed"
    assert note["decisions"][0]["atMs"] == 2000
    a = note["actions"][0]
    assert a["assignee"] is None and a["dueDate"] == "来週" and a["status"] == "open" and a["noEvidenceReason"]
    assert note["openQuestions"] == [] and ctx.dropped_ids == 1
    # previous note -> draft -> note keeps item ids through refs
    segs2 = _segs(2)
    table2 = build_alias_table(context=[], transcript=segs2, previous_segments=segs)
    prev_draft = note_to_draft(note, table2)
    assert prev_draft is not None and prev_draft["decisions"][0]["ref"] == "D1"
    assert prev_draft["bullets"][0]["evidence"] == ["P2"]
    ctx2 = MapContext(meeting_id=mid, revision_id=new_id(), generated_at="2026-09-30T10:00:00.000+09:00",
                      coverage_from_ms=0, coverage_to_ms=5000, section_from_ms=3000, section_to_ms=5000,
                      previous_note=note, table=table2)
    note2 = draft_to_note(prev_draft | {"cornell": {**prev_draft["cornell"], "sectionSummary": "x"}}, ctx2)
    assert note2["decisions"][0]["id"] == note["decisions"][0]["id"]
    assert note2["bullets"][0]["evidenceSegmentIds"] == [segs[1]["id"]]
    assert [s["summary"] for s in note2["cornell"]["sections"]] == ["sec", "x"]


def test_extract_json_object_variants() -> None:
    assert extract_json_object('{"a": 1}') == {"a": 1}
    assert extract_json_object('```json\n{"a": 2}\n```') == {"a": 2}
    assert extract_json_object('Here you go: {"a": 3} thanks') == {"a": 3}
    with pytest.raises(ValueError):
        extract_json_object("no json")


# ------------------------------------------------------------------ crypto / CLI
def test_audio_cipher_roundtrip_and_key_destruction() -> None:
    store = MemorySecretStore()
    c = AudioCipher(store)
    mid = new_id()
    blob = c.encrypt(mid, b"pcm-bytes")
    assert blob.startswith(b"SKA1") and b"pcm-bytes" not in blob
    assert c.decrypt(mid, blob) == b"pcm-bytes"
    with pytest.raises(Exception):  # noqa: B017 - AAD binds the blob to its meeting
        c.decrypt(new_id(), blob)
    assert c.destroy_key(mid)
    with pytest.raises(KeyError):
        c.decrypt(mid, blob)


def test_cli_detection_and_missing_required_flags(tmp_path) -> None:
    control = tmp_path / "c.json"
    control.write_text('{"help": "Usage: claude\\n  -p, --print\\n  --output-format <f>\\n"}', encoding="utf-8")
    cli = ClaudeCli(workdir=tmp_path / "w", cli_path=sys.executable,
                    prefix_args=[str(FAKE_CLI), "--control", str(control)])
    info = asyncio.run(cli.detect())
    assert info.version == "9.9.9" and info.state == "unsupported"  # --tools missing: refuse to run

    async def run() -> None:
        with pytest.raises(CliError) as exc:
            await cli.run("{}", "x", key="k")
        assert exc.value.code == "cli_not_found"

    asyncio.run(run())
    missing = ClaudeCli(workdir=tmp_path / "w", cli_path=str(tmp_path / "nope.exe"))
    assert asyncio.run(missing.detect()).state == "cli_not_found"
