"""Aggregate frame probabilities into speech candidates (spec §4 VAD と発話確定).

* 300 ms padding before/after speech
* ~500 ms of continuous non-speech ends a candidate
* candidates are force-split at ``max_segment_ms`` so STT windows stay bounded
* a partial request is emitted every ``partial_every_ms`` of ongoing speech

All positions are absolute sample indices on the 16 kHz derived stream.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SR = 16_000
FRAME = 512


@dataclass
class VadParams:
    pad_ms: int = 300
    end_silence_ms: int = 500
    min_speech_ms: int = 160
    max_segment_ms: int = 25_000
    partial_every_ms: int = 1_500


@dataclass
class SpeechStart:
    start: int  # padded start sample
    speech_start: int  # first speech frame


@dataclass
class SpeechPartial:
    start: int
    end: int


@dataclass
class SpeechEnd:
    start: int  # padded start
    end: int  # padded end
    speech_end: int  # end of last speech frame (unpadded) -> "VAD 発話終了"
    forced: bool = False


class Segmenter:
    def __init__(self, threshold: float, params: VadParams | None = None) -> None:
        self.p = params or VadParams()
        self.threshold = threshold
        self.frame = FRAME
        self._pad = int(self.p.pad_ms * SR / 1000)
        self._end_frames = max(1, int(np.ceil(self.p.end_silence_ms * SR / 1000 / FRAME)))
        self._min_frames = max(1, int(np.ceil(self.p.min_speech_ms * SR / 1000 / FRAME)))
        self._max_len = int(self.p.max_segment_ms * SR / 1000)
        self._partial_every = int(self.p.partial_every_ms * SR / 1000)
        self.in_speech = False
        self._run = 0  # consecutive speech frames before start
        self._run_start = 0
        self._silence = 0
        self._seg_start = 0
        self._last_speech_end = 0
        self._last_partial = 0
        self.floor = 0  # samples before this index are not available (e.g. meeting start)

    def process(self, start_index: int, probs: np.ndarray) -> list[object]:
        events: list[object] = []
        for i, prob in enumerate(probs):
            f_start = start_index + i * self.frame
            f_end = f_start + self.frame
            speech = bool(prob >= self.threshold)
            if not self.in_speech:
                if speech:
                    if self._run == 0:
                        self._run_start = f_start
                    self._run += 1
                    if self._run >= self._min_frames:
                        self.in_speech = True
                        self._seg_start = max(self.floor, self._run_start - self._pad)
                        self._last_speech_end = f_end
                        self._silence = 0
                        self._last_partial = f_end
                        events.append(SpeechStart(start=self._seg_start, speech_start=self._run_start))
                else:
                    self._run = 0
                continue
            # in speech
            if speech:
                self._silence = 0
                self._last_speech_end = f_end
            else:
                self._silence += 1
            if self._silence >= self._end_frames:
                end = self._last_speech_end + self._pad
                events.append(SpeechEnd(start=self._seg_start, end=min(end, f_end),
                                        speech_end=self._last_speech_end))
                self._reset()
                continue
            if f_end - self._seg_start >= self._max_len:
                events.append(SpeechEnd(start=self._seg_start, end=f_end, speech_end=f_end, forced=True))
                # Continue speech immediately without padding overlap.
                self._seg_start = f_end
                self._last_partial = f_end
                events.append(SpeechStart(start=f_end, speech_start=f_end))
                continue
            if f_end - self._last_partial >= self._partial_every:
                self._last_partial = f_end
                events.append(SpeechPartial(start=self._seg_start, end=f_end))
        return events

    def flush(self, end_index: int) -> list[object]:
        """Close any open candidate (stop / end of stream)."""
        if not self.in_speech:
            self._run = 0
            return []
        end = min(self._last_speech_end + self._pad, end_index)
        ev = SpeechEnd(start=self._seg_start, end=max(end, self._seg_start), speech_end=self._last_speech_end,
                       forced=True)
        self._reset()
        return [ev]

    def _reset(self) -> None:
        self.in_speech = False
        self._run = 0
        self._silence = 0
