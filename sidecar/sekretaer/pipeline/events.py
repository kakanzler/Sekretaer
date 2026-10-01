"""Event bus: persists replayable events (with a global monotonically increasing seq) and fans them out
to WebSocket subscribers. ``transcript.partial`` and ``audio.level`` are ephemeral (not stored)."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from typing import Any

from ..logsetup import log
from ..storage.db import Database
from ..util import dumps, loads, new_id, now_iso

EPHEMERAL_EVENTS = frozenset({"transcript.partial", "audio.level"})
EVENT_NAMES = frozenset({
    "meeting.state", "transcript.partial", "transcript.final", "summary.queued", "summary.updated", "job.failed",
    "audio.gap", "audio.level", "privacy.state", "warning",
})
RETAIN_EVENTS = 10_000
_PRUNE_EVERY = 500

logger = logging.getLogger("sekretaer.events")


class Subscriber:
    def __init__(self, maxsize: int = 2000) -> None:
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=maxsize)
        self.overflowed = False

    def push(self, event: dict[str, Any]) -> None:
        if self.overflowed:
            return
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            # The client is too slow; it gets disconnected and reconnects with its cursor.
            self.overflowed = True
            try:
                self.queue.put_nowait(None)
            except asyncio.QueueFull:
                pass


class EventBus:
    def __init__(self, db: Database, retain: int = RETAIN_EVENTS) -> None:
        self.db = db
        self.retain = retain
        self._lock = threading.Lock()
        self._seq = int(db.scalar("SELECT COALESCE(MAX(seq), 0) FROM events") or 0)
        self._subs: set[Subscriber] = set()
        self._since_prune = 0
        self.loop: asyncio.AbstractEventLoop | None = None

    def publish(self, event: str, meeting_id: str | None, data: dict[str, Any]) -> dict[str, Any]:
        if event not in EVENT_NAMES:
            raise ValueError(f"unknown event {event}")
        with self._lock:
            self._seq += 1
            env = {
                "event": event,
                "eventId": new_id(),
                "meetingId": meeting_id,
                "seq": self._seq,
                "occurredAt": now_iso(),
                "data": data,
            }
            if event not in EPHEMERAL_EVENTS:
                try:
                    self.db.execute(
                        "INSERT INTO events(seq,event_id,event,meeting_id,occurred_at,data_json) VALUES (?,?,?,?,?,?)",
                        (env["seq"], env["eventId"], event, meeting_id, env["occurredAt"], dumps(data)),
                    )
                    self._since_prune += 1
                    if self._since_prune >= _PRUNE_EVERY:
                        self._since_prune = 0
                        self.db.execute("DELETE FROM events WHERE seq <= ?", (self._seq - self.retain,))
                except sqlite3.Error:
                    # Still deliver live; the replay gap is reported through the warning path by callers.
                    log(logger, logging.ERROR, "event persist failed", event=event, code="db_write_failed")
        self._fanout(env)
        return env

    def _fanout(self, env: dict[str, Any]) -> None:
        loop = self.loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is not None and running is not loop:
            loop.call_soon_threadsafe(self._deliver, env)
        else:
            self._deliver(env)

    def _deliver(self, env: dict[str, Any]) -> None:
        for sub in list(self._subs):
            sub.push(env)

    def subscribe(self) -> Subscriber:
        sub = Subscriber()
        self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self._subs.discard(sub)

    def replay(self, cursor: int, limit: int = RETAIN_EVENTS) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT seq,event_id,event,meeting_id,occurred_at,data_json FROM events WHERE seq > ? ORDER BY seq LIMIT ?",
            (cursor, limit),
        )
        return [
            {
                "event": r["event"],
                "eventId": r["event_id"],
                "meetingId": r["meeting_id"],
                "seq": r["seq"],
                "occurredAt": r["occurred_at"],
                "data": loads(r["data_json"], {}),
            }
            for r in rows
        ]

    @property
    def last_seq(self) -> int:
        return self._seq
