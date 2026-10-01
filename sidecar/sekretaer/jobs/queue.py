"""Persistent summary job queue (spec §6, §9, appendix A).

* status: queued -> running -> succeeded | failed | canceled; transient failures go to retry_wait
* concurrency 1, per-meeting ordering (a later job of a meeting waits for earlier ones)
* backoff 2 s, 4 s, 8 s ... capped at 60 s, at most ``max_attempts`` (3) attempts
* consent / input / permission / missing-CLI / auth errors are not retried automatically
* on startup ``running`` jobs return to ``queued``
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from ..logsetup import log
from ..pipeline.events import EventBus
from ..storage.repo import Repo
from ..summarization.cli import CliError
from ..summarization.service import Summarizer
from ..util import iso, now_dt, now_iso, parse_iso

logger = logging.getLogger("sekretaer.jobs")

RETRYABLE = frozenset({"cli_timeout", "summary_invalid_schema", "cli_failed"})
TERMINAL = frozenset({"succeeded", "failed", "canceled"})
MAX_ATTEMPTS = 3


def backoff_seconds(attempt: int) -> float:
    """attempt = number of attempts made so far (1-based) -> 2, 4, 8, ... <= 60."""
    return float(min(60, 2 ** max(1, attempt)))


def job_to_api(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": job["id"],
        "meetingId": job["meeting_id"],
        "trigger": job["trigger"],
        "status": job["status"],
        "attempt": job["attempt"],
        "maxAttempts": job["max_attempts"],
        "errorCode": job["error_code"],
        "retryAfter": job["next_retry_at"] if job["status"] == "retry_wait" else None,
        "createdAt": job["created_at"],
        "startedAt": job["started_at"],
        "finishedAt": job["finished_at"],
        "rangeStartMs": job["range_start_ms"],
        "rangeEndMs": job["range_end_ms"],
        "resultRevisionId": job["result_revision_id"],
    }


class JobQueue:
    def __init__(
        self, repo: Repo, bus: EventBus, summarizer: Summarizer,
        apply_note: Callable[[dict[str, Any], Any], dict[str, Any]],
        on_meeting_changed: Callable[[str], None] | None = None,
    ) -> None:
        self.repo = repo
        self.bus = bus
        self.summarizer = summarizer
        self.apply_note = apply_note
        self.on_meeting_changed = on_meeting_changed
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._running_job: str | None = None
        self.paused = False  # test hook: hold jobs in the queue

    # ------------------------------------------------------------------ lifecycle
    def recover_on_startup(self) -> int:
        rows = self.repo.db.query("SELECT id FROM summary_jobs WHERE status='running'")
        for r in rows:
            self.repo.update_job(r["id"], status="queued", started_at=None)
        return len(rows)

    def start(self) -> None:
        if self._task is None:
            self._stopping = False
            self._task = asyncio.create_task(self._loop(), name="summary-job-worker")

    async def stop(self) -> None:
        self._stopping = True
        self.summarizer.cli.kill_all()
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110
                pass
            self._task = None
        # A job interrupted by shutdown is retried on the next start.
        if self._running_job is not None:
            self.repo.update_job(self._running_job, status="queued", started_at=None)
            self._running_job = None

    def wake(self) -> None:
        self._wake.set()

    # ------------------------------------------------------------------ operations
    def retry(self, job_id: str) -> dict[str, Any]:
        job = self.repo.get_job(job_id)
        assert job is not None
        if job["status"] in ("failed", "canceled", "retry_wait"):
            self.summarizer.cli.release(job_id)
            self.repo.update_job(job_id, status="queued", attempt=0, error_code=None, next_retry_at=None,
                                 finished_at=None)
            self.wake()
            self._changed(job["meeting_id"])
        return self.repo.get_job(job_id) or job

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.repo.get_job(job_id)
        assert job is not None
        if job["status"] in ("queued", "retry_wait"):
            self.repo.update_job(job_id, status="canceled", finished_at=now_iso(), next_retry_at=None)
        elif job["status"] == "running":
            self.summarizer.cli.cancel(job_id)
            self.repo.update_job(job_id, status="canceled", finished_at=now_iso())
        self._changed(job["meeting_id"])
        return self.repo.get_job(job_id) or job

    def cancel_meeting(self, meeting_id: str) -> int:
        n = 0
        for job in self.repo.list_jobs(meeting_id):
            if job["status"] in ("queued", "retry_wait", "running"):
                self.cancel(job["id"])
                n += 1
        return n

    def _changed(self, meeting_id: str) -> None:
        if self.on_meeting_changed is not None:
            self.on_meeting_changed(meeting_id)

    # ------------------------------------------------------------------ worker
    def _next_runnable(self) -> tuple[dict[str, Any] | None, float | None]:
        now = now_dt()
        seen_meetings: set[str] = set()
        soonest: float | None = None
        for job in self.repo.runnable_jobs():
            mid = job["meeting_id"]
            if mid in seen_meetings:
                continue  # per-meeting ordering: only the oldest unfinished job of a meeting may run
            seen_meetings.add(mid)
            if job["status"] == "queued":
                return job, None
            if job["status"] == "retry_wait":
                due = parse_iso(job["next_retry_at"])
                if due is None or due <= now:
                    return job, None
                wait = (due - now).total_seconds()
                soonest = wait if soonest is None else min(soonest, wait)
        return None, soonest

    async def _loop(self) -> None:
        while not self._stopping:
            job, wait = (None, None) if self.paused else self._next_runnable()
            if job is None:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=min(wait or 1.0, 5.0))
                except TimeoutError:
                    pass
                continue
            try:
                await self._run(job)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a bug in one job must not kill the worker
                logger.exception("job crashed", extra={"fields": {"jobId": job["id"]}})
                self._finish_failure(job, "internal_error", retryable=False)

    async def _run(self, job: dict[str, Any]) -> None:
        meeting = self.repo.get_meeting(job["meeting_id"])
        if meeting is None or meeting["state"] in ("deleting", "deleted"):
            self.repo.update_job(job["id"], status="canceled", finished_at=now_iso())
            return
        consent = self.repo.consent_state(job["meeting_id"])
        if consent["external_processing"] != "granted" or not meeting["settings"].get("summarizationEnabled"):
            self._finish_failure(job, "consent_required", retryable=False)
            return
        attempt = int(job["attempt"]) + 1
        self.repo.update_job(job["id"], status="running", attempt=attempt, started_at=now_iso(), error_code=None,
                             next_retry_at=None)
        self._running_job = job["id"]
        self._changed(job["meeting_id"])
        log(logger, logging.INFO, "job started", jobId=job["id"], meetingId=job["meeting_id"], attempt=attempt,
            trigger=job["trigger"])
        try:
            try:
                output = await self.summarizer.run(job)
            finally:
                self.summarizer.cli.release(job["id"])
        except CliError as exc:
            self._running_job = None
            current = self.repo.get_job(job["id"])
            if exc.code == "canceled" or (current and current["status"] == "canceled"):
                self.repo.update_job(job["id"], status="canceled", finished_at=now_iso())
                self._changed(job["meeting_id"])
                return
            self._finish_failure({**job, "attempt": attempt}, exc.code, retryable=exc.code in RETRYABLE)
            return
        self._running_job = None
        current = self.repo.get_job(job["id"])
        if current is None or current["status"] == "canceled":
            return
        if output is None:
            # Nothing new since the note's coverage (e.g. a later job already covered this range).
            self.repo.update_job(job["id"], status="succeeded", finished_at=now_iso())
            self._changed(job["meeting_id"])
            return
        try:
            revision = self.apply_note(job, output)
        except Exception:  # noqa: BLE001
            logger.exception("apply failed", extra={"fields": {"jobId": job["id"]}})
            self._finish_failure({**job, "attempt": attempt}, "db_write_failed", retryable=True)
            return
        self.repo.update_job(job["id"], status="succeeded", finished_at=now_iso(),
                             result_revision_id=revision["id"])
        self.bus.publish("summary.updated", job["meeting_id"], {
            "jobId": job["id"], "revisionId": revision["id"], "conflict": revision["status"] == "pending_conflict",
        })
        self._changed(job["meeting_id"])

    def _finish_failure(self, job: dict[str, Any], code: str, *, retryable: bool) -> None:
        attempt = int(job["attempt"])
        max_attempts = int(job.get("max_attempts") or MAX_ATTEMPTS)
        will_retry = retryable and attempt < max_attempts
        retry_after: str | None = None
        if will_retry:
            retry_after = iso(now_dt() + timedelta(seconds=backoff_seconds(attempt)))
            self.repo.update_job(job["id"], status="retry_wait", error_code=code, next_retry_at=retry_after)
        else:
            self.repo.update_job(job["id"], status="failed", error_code=code, finished_at=now_iso(),
                                 next_retry_at=None)
        self.repo.update_meeting(job["meeting_id"], last_error=code)
        self.bus.publish("job.failed", job["meeting_id"], {
            "jobId": job["id"], "errorCode": code, "retryAfter": retry_after, "willRetry": will_retry,
        })
        log(logger, logging.WARNING, "job failed", jobId=job["id"], errorCode=code, attempt=attempt,
            willRetry=will_retry)
        self._changed(job["meeting_id"])
        self.wake()
