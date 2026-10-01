"""Summary breakpoint policy (spec §4 要約区切りポリシー). Pure logic, driven by audio time.

| trigger     | condition                                                                     |
|-------------|-------------------------------------------------------------------------------|
| silence     | 1.2 s after VAD speech end (longer when the last utterance looks unfinished)  |
|             | AND >= 15 s of unsummarized speech                                            |
| long_speech | unsummarized final speech reaches 90 s without a silence break                 |
| manual      | user action (handled by the session: waits for in-flight STT, then enqueues)  |
| final       | after stop, once pending STT is finalized (handled by the session)            |

Automatic triggers are suppressed while the text since the last summary is shorter than
``min_text_chars`` or less than ``min_interval_ms`` of meeting time has passed since the last job.
Semantic completion uses lightweight Japanese/English rules; no LLM call is made.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_COMPLETE = re.compile(
    r"([。．！？!?]|(です|ます|ました|でした|ません|でしょう|ましょう|ください|お願いします|だ|ね|よ|か)[。．]?|"
    r"\b(ok|okay|thanks|thank you|agreed|done)[.!]?|[a-z0-9][.!?])\s*$",
    re.IGNORECASE,
)
_INCOMPLETE = re.compile(
    r"([、，,]|(けど|けれど|けれども|ので|から|が|て|で|し|とか|たり|ですが|ますが|という|and|but|so|because|or|"
    r"the|to|of|with|um|uh|えー|あの|その))\s*$",
    re.IGNORECASE,
)
_DECISION = re.compile(
    r"(決定|決まり|決めました|合意|承認|にしましょう|で行きましょう|でいきましょう|進めます|進めましょう|"
    r"お願いします|担当します|やります|\bagreed\b|\bdecided\b|\blet's go with\b|\bwe will\b|\bwill do\b)",
    re.IGNORECASE,
)


def completion_cue(text: str) -> str:
    """Return 'complete', 'incomplete' or 'neutral' for the last utterance before a pause."""
    t = text.strip()
    if not t:
        return "neutral"
    if _DECISION.search(t[-40:]) or _COMPLETE.search(t):
        return "complete"
    if _INCOMPLETE.search(t):
        return "incomplete"
    return "neutral"


@dataclass
class BreakpointPolicy:
    silence_after_vad_ms: int = 1_200
    incomplete_extra_ms: int = 1_300
    min_unsummarized_ms: int = 15_000
    long_speech_ms: int = 90_000
    min_text_chars: int = 40
    min_interval_ms: int = 20_000


@dataclass
class _Candidate:
    speech_end_ms: int  # unpadded VAD speech end
    range_end_ms: int  # padded end = end of the segment that closed
    silence_until_ms: int | None = None  # set when speech resumed


@dataclass
class _Final:
    start_ms: int
    end_ms: int
    text: str


@dataclass
class Trigger:
    kind: str  # silence | long_speech
    range_end_ms: int


@dataclass
class BreakpointDetector:
    policy: BreakpointPolicy = field(default_factory=BreakpointPolicy)
    cursor_ms: int = 0  # end of the range claimed by the last job
    last_job_at_ms: int = 0
    _active_sources: set[str] = field(default_factory=set)
    _candidates: list[_Candidate] = field(default_factory=list)
    _finals: list[_Final] = field(default_factory=list)

    # ------------------------------------------------------------------ inputs
    def on_speech_start(self, source: str, ms: int) -> None:
        self._active_sources.add(source)
        for c in self._candidates:
            if c.silence_until_ms is None and ms >= c.speech_end_ms:
                c.silence_until_ms = ms

    def on_speech_end(self, source: str, speech_end_ms: int, range_end_ms: int) -> None:
        self._active_sources.discard(source)
        if self._active_sources:
            return  # another source is still talking: no silence yet
        self._candidates.append(_Candidate(speech_end_ms=speech_end_ms, range_end_ms=range_end_ms))

    def on_final(self, start_ms: int, end_ms: int, text: str) -> None:
        if end_ms <= self.cursor_ms or not text.strip():
            return
        self._finals.append(_Final(start_ms, end_ms, text))
        self._finals.sort(key=lambda f: f.end_ms)

    def on_job_created(self, range_end_ms: int, at_ms: int) -> None:
        self.cursor_ms = max(self.cursor_ms, range_end_ms)
        self.last_job_at_ms = max(self.last_job_at_ms, at_ms)
        self._finals = [f for f in self._finals if f.end_ms > self.cursor_ms]
        self._candidates = [c for c in self._candidates if c.range_end_ms > self.cursor_ms]

    # ------------------------------------------------------------------ evaluation
    def _unsummarized(self, until_ms: int) -> list[_Final]:
        return [f for f in self._finals if self.cursor_ms < f.end_ms <= until_ms]

    def _passes_suppression(self, finals: list[_Final], at_ms: int) -> bool:
        chars = sum(len(f.text.strip()) for f in finals)
        return chars >= self.policy.min_text_chars and at_ms - self.last_job_at_ms >= self.policy.min_interval_ms

    def evaluate(self, now_ms: int, stt_pending_until: int | None) -> Trigger | None:
        """``now_ms`` = audio time processed on every source; ``stt_pending_until`` = end of the earliest
        segment still waiting for final STT (None if nothing pending)."""
        p = self.policy
        keep: list[_Candidate] = []
        trigger: Trigger | None = None
        for c in self._candidates:
            if trigger is not None:
                keep.append(c)
                continue
            if stt_pending_until is not None and stt_pending_until <= c.range_end_ms:
                keep.append(c)  # the utterance before the pause is not transcribed yet
                continue
            finals = self._unsummarized(c.range_end_ms)
            last_text = finals[-1].text if finals else ""
            required = p.silence_after_vad_ms
            if completion_cue(last_text) == "incomplete":
                required += p.incomplete_extra_ms
            silence_end = c.silence_until_ms if c.silence_until_ms is not None else now_ms
            silence = silence_end - c.speech_end_ms
            if silence < required:
                if c.silence_until_ms is None:
                    keep.append(c)  # still silent; wait
                continue  # speech resumed too early: not a breakpoint
            if not finals:
                continue
            span = c.range_end_ms - finals[0].start_ms
            at_ms = c.speech_end_ms + required
            if span >= p.min_unsummarized_ms and self._passes_suppression(finals, at_ms):
                trigger = Trigger("silence", c.range_end_ms)
            # candidates that do not qualify are dropped; later speech accumulates for the next one
        self._candidates = keep
        if trigger is not None:
            return trigger
        finals = self._unsummarized(10**12)
        if finals:
            last_end = finals[-1].end_ms
            if (stt_pending_until is None or stt_pending_until > last_end) and \
                    last_end - finals[0].start_ms >= p.long_speech_ms and self._passes_suppression(finals, last_end):
                return Trigger("long_speech", last_end)
        return None
