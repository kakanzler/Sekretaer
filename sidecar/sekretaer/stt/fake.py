"""Deterministic STT used by tests and synthetic demos. Returns scripted utterances in order."""

from __future__ import annotations

import threading
import time

import numpy as np

from .base import SttEngine, SttSegment

DEFAULT_SCRIPT = [
    "それでは定例会議を始めます。",
    "まず先週の試作の進捗について共有します。",
    "試作の第一版は予定どおり完成しました。",
    "次に評価の日程を決めたいと思います。",
    "評価は来週金曜に実施することで合意しました。",
    "田中さんが評価手順書を来週水曜までに作成します。",
    "予算の上限についてはまだ確認が必要です。",
    "佐藤さんが経理に確認して次回報告します。",
    "ほかに議題はありますか。",
    "特にないので本日はここまでにしましょう。",
]


class ScriptedStt(SttEngine):
    name = "scripted"
    model = "scripted"

    def __init__(self, script: list[str] | None = None, delay_s: float = 0.0, language: str = "ja") -> None:
        self.script = list(script or DEFAULT_SCRIPT)
        self.delay_s = delay_s
        self.lang = language
        self._i = 0
        self._lock = threading.Lock()
        self.calls = 0
        self.partial_calls = 0
        self.fail_next = 0

    @property
    def state(self) -> str:
        return "ready"

    def transcribe(self, audio: np.ndarray, language: str | None, *, partial: bool) -> list[SttSegment]:
        if self.delay_s:
            time.sleep(self.delay_s)
        dur = audio.size / 16_000
        with self._lock:
            if partial:
                self.partial_calls += 1
                text = self.script[self._i % len(self.script)]
                cut = max(1, int(len(text) * 0.5))
                return [SttSegment(text=text[:cut], start_s=0.0, end_s=dur, language=language or self.lang)]
            self.calls += 1
            text = self.script[self._i % len(self.script)]
            self._i += 1
        return [SttSegment(text=text, start_s=0.0, end_s=dur, avg_logprob=-0.25, no_speech_prob=0.02,
                           language=language or self.lang)]
