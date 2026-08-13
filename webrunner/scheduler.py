"""The scheduling loop: decides what runs when, and supervises each run."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import cronparse, notify
from .bus import bus, publish_event
from .config import Config
from .db import Database
from .engine import RunCancelled, RunOutcome, WorkflowEngine
from .process import terminate_tree

log = logging.getLogger("webrunner.scheduler")

# A run that should have started more than this long ago is considered missed;
# without catch-up enabled it is skipped rather than fired late.
MISFIRE_GRACE_SECONDS = 120.0
FAILURE_STATUSES = {"failed", "timeout", "error"}


@dataclass
class ActiveRun:
    run_id: int
    job_id: int | None
    job_name: str
    started_at: float
    task: asyncio.Task | None = None
    pids: set[int] = field(default_factory=set)


class Scheduler:
    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config
        self.engine = WorkflowEngine(db, config)
        self.active: dict[int, ActiveRun] = {}
        self._job_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._queued: dict[int, int] = defaultdict(int)
        self._loop_task: asyncio.Task | None = None
        self._pending: set[asyncio.Task] = set()
        self._stopping = False
        self.started_at = time.time()

    # ----------------------------------------------------------- lifecycle

    async def start(self) -> None:
        orphaned = self.db.mark_orphaned_runs()
        if orphaned:
            log.warning("marked %d interrupted run(s) from a previous process", orphaned)
        self.reschedule_all()
        self._stopping = False
        self._loop_task = asyncio.create_task(self._loop(), name="webrunner-scheduler")
        log.info("scheduler started (%d job(s))", len(self.db.list_jobs()))

    async def stop(self, timeout: float = 10.0) -> None:
        self._stopping = True
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        for active in list(self.active.values()):
            if active.task and not active.task.done():
                active.task.cancel()
        pending = [a.task for a in self.active.values() if a.task]
        if pending:
            await asyncio.wait(pending, timeout=timeout)
        for task in list(self._pending):
            task.cancel()
        log.info("scheduler stopped")

    # ------------------------------------------------------------ timezone

    def timezone_for(self, job: dict | None = None) -> tzinfo:
        """Resolve job → global setting → server default → system local.

        Never raises: a Windows host without the ``tzdata`` package has no
        time-zone database at all, and a scheduler that throws there would be
        worse than one that quietly runs in local time.
        """
        candidates = [
            (job or {}).get("timezone") or "",
            self.db.get_setting("timezone", ""),
            self.config.timezone,
        ]
        for name in candidates:
            name = (name or "").strip()
            if not name or name.lower() == "local":
                continue
            try:
                return ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError, KeyError, OSError):
                log.warning("unknown timezone %r, falling back", name)
        local = datetime.now().astimezone().tzinfo
        return local or timezone.utc

    def _to_epoch(self, naive_local: datetime, tz: tzinfo) -> float:
        return naive_local.replace(tzinfo=tz).timestamp()

    def _parse_boundary(self, value: str, tz: tzinfo) -> float | None:
        value = (value or "").strip()
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=tz).timestamp()
        return parsed.timestamp()

    # ---------------------------------------------------------- scheduling

    def compute_next_run(self, job: dict, after: float | None = None) -> float | None:
        """Next fire time as a unix timestamp, or None if the job never fires again."""
        trigger = (job.get("trigger_type") or "cron").lower()
        if trigger == "manual" or not job.get("enabled", True):
            return None

        tz = self.timezone_for(job)
        now = time.time()
        reference = after if after is not None else now
        start_at = self._parse_boundary(job.get("start_at", ""), tz)
        end_at = self._parse_boundary(job.get("end_at", ""), tz)
        if start_at and reference < start_at:
            reference = start_at - 1

        candidate: float | None = None
        if trigger == "cron":
            expression = (job.get("cron") or "").strip()
            if not expression:
                return None
            local_reference = datetime.fromtimestamp(reference, tz).replace(tzinfo=None)
            try:
                candidate = self._to_epoch(
                    cronparse.CronExpr(expression).next_after(local_reference), tz
                )
            except cronparse.CronError as exc:
                log.error("job %r has an invalid cron expression: %s", job.get("name"), exc)
                return None

        elif trigger == "interval":
            try:
                seconds = float(job.get("interval_seconds") or 0)
            except (TypeError, ValueError):
                seconds = 0.0
            if seconds <= 0:
                return None
            candidate = reference + seconds
            if start_at and candidate < start_at:
                candidate = start_at

        elif trigger == "date":
            candidate = self._parse_boundary(job.get("run_at", ""), tz)
            if candidate is None or candidate <= (after if after is not None else now - 1):
                return None

        if candidate is None:
            return None
        if end_at and candidate > end_at:
            return None
        return candidate

    def reschedule(self, job_or_id: dict | int, after: float | None = None) -> float | None:
        job = self.db.get_job(job_or_id) if isinstance(job_or_id, int) else job_or_id
        if not job:
            return None
        next_run = self.compute_next_run(job, after=after)
        self.db.set_next_run(job["id"], next_run)
        return next_run

    def reschedule_all(self) -> None:
        now = time.time()
        for job in self.db.list_jobs():
            if not job["enabled"]:
                self.db.set_next_run(job["id"], None)
                continue
            existing = job.get("next_run")
            if existing and existing > now:
                continue
            if existing and job.get("catch_up"):
                # Leave it in the past so the next tick fires it immediately.
                continue
            self.reschedule(job)

    def preview_runs(self, job: dict, count: int = 5) -> list[float]:
        """Upcoming fire times, used by the UI's schedule preview."""
        results: list[float] = []
        cursor = time.time()
        for _ in range(max(0, count)):
            nxt = self.compute_next_run(job, after=cursor)
            if nxt is None:
                break
            results.append(nxt)
            cursor = nxt
        return results

    # ----------------------------------------------------------- main loop

    async def _loop(self) -> None:
        prune_at = time.time() + 3600
        while not self._stopping:
            try:
                await asyncio.sleep(self.config.tick_seconds)
                now = time.time()
                for job in self.db.due_jobs(now):
                    await self._dispatch(job, now)

                if now >= prune_at:
                    prune_at = now + 3600
                    keep = int(self.db.get_setting("retention_runs", "5000") or 5000)
                    removed = self.db.prune_runs(keep)
                    if removed:
                        log.info("pruned %d old run(s)", removed)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must never die
                log.exception("scheduler tick failed")
                await asyncio.sleep(1.0)

    async def _dispatch(self, job: dict, now: float) -> None:
        scheduled_for = job.get("next_run") or now
        overdue = now - scheduled_for

        next_run = self.compute_next_run(job, after=max(scheduled_for, now))
        self.db.set_next_run(job["id"], next_run)

        if overdue > MISFIRE_GRACE_SECONDS and not job.get("catch_up"):
            log.warning(
                "job %r missed its %s slot by %.0fs - skipping (enable catch-up to run it late)",
                job["name"], datetime.fromtimestamp(scheduled_for).isoformat(" ", "seconds"), overdue,
            )
            publish_event("job.missed", job_id=job["id"], job_name=job["name"],
                          scheduled_for=scheduled_for)
            return

        await self.trigger(job, trigger="catchup" if overdue > MISFIRE_GRACE_SECONDS else "schedule")

    # ------------------------------------------------------------- running

    async def trigger(self, job: dict, trigger: str = "manual") -> int | None:
        """Start a run, honouring the job's concurrency policy."""
        job_id = job["id"]
        policy = (job.get("concurrency") or "skip").lower()
        running = [a for a in self.active.values() if a.job_id == job_id]

        if running and policy == "skip":
            run_id = self.db.create_run(job, trigger, status="skipped")
            self.db.finish_run(
                run_id, finished_at=time.time(), duration=0.0, exit_code=None,
                error=f"skipped: already running (run #{running[0].run_id})",
            )
            publish_event("run.skipped", job_id=job_id, job_name=job["name"], run_id=run_id)
            log.info("job %r already running - skipped", job["name"])
            return run_id

        if running and policy == "replace":
            for active in running:
                await self.cancel_run(active.run_id, reason="replaced by a newer run")

        if policy == "queue" and self._queued[job_id] >= self.config.max_queued_per_job:
            run_id = self.db.create_run(job, trigger, status="skipped")
            self.db.finish_run(
                run_id, finished_at=time.time(), duration=0.0,
                error=f"skipped: queue is full ({self.config.max_queued_per_job})",
            )
            return run_id

        run_id = self.db.create_run(job, trigger)
        active = ActiveRun(run_id=run_id, job_id=job_id, job_name=job["name"], started_at=time.time())
        self.active[run_id] = active
        task = asyncio.create_task(
            self._supervise(job, run_id, trigger, active), name=f"run-{run_id}"
        )
        active.task = task
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

        publish_event("run.started", job_id=job_id, job_name=job["name"],
                      run_id=run_id, trigger=trigger)
        return run_id

    async def _supervise(self, job: dict, run_id: int, trigger: str, active: ActiveRun) -> None:
        policy = (job.get("concurrency") or "skip").lower()
        attempt = 1
        try:
            if policy == "queue":
                self._queued[job["id"]] += 1
                try:
                    async with self._job_locks[job["id"]]:
                        await self._run_with_retries(job, run_id, trigger, active, attempt)
                finally:
                    self._queued[job["id"]] = max(0, self._queued[job["id"]] - 1)
            else:
                await self._run_with_retries(job, run_id, trigger, active, attempt)
        finally:
            # Retries re-register the same ActiveRun under a fresh run id, so
            # clear every id that points at it — popping only the original
            # would leave the job looking busy forever.
            for stale in [key for key, value in self.active.items() if value is active]:
                self.active.pop(stale, None)

    async def _run_with_retries(
        self, job: dict, run_id: int, trigger: str, active: ActiveRun, attempt: int
    ) -> None:
        max_retries = int(job.get("max_retries") or 0)
        delay = max(0, int(job.get("retry_delay_seconds") or 0))

        while True:
            outcome = await self._execute(job, run_id, trigger, active, attempt)
            if outcome.status not in FAILURE_STATUSES or attempt > max_retries:
                return
            attempt += 1
            log.info(
                "job %r failed, retrying (%d/%d) in %ds", job["name"], attempt - 1, max_retries, delay
            )
            if delay:
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    return
            fresh = self.db.get_job(job["id"]) or job
            self.active.pop(active.run_id, None)      # the finished attempt
            run_id = self.db.create_run(fresh, "retry", attempt=attempt)
            active.run_id = run_id
            active.pids.clear()
            self.active[run_id] = active
            publish_event("run.started", job_id=job["id"], job_name=job["name"],
                          run_id=run_id, trigger="retry")
            job = fresh
            trigger = "retry"

    async def _execute(
        self, job: dict, run_id: int, trigger: str, active: ActiveRun, attempt: int
    ) -> RunOutcome:
        started = time.time()

        def on_process(process) -> None:
            if process.pid:
                active.pids.add(process.pid)

        try:
            outcome = await self.engine.execute(job, run_id, trigger, on_process=on_process)
        except RunCancelled as cancelled:
            outcome = cancelled.outcome
        except asyncio.CancelledError:
            outcome = RunOutcome(status="cancelled", error="cancelled", exit_code=None)
        except Exception as exc:  # noqa: BLE001
            log.exception("run %s crashed", run_id)
            outcome = RunOutcome(status="error", error=f"{type(exc).__name__}: {exc}", exit_code=-1)

        finished = time.time()
        self.db.finish_run(
            run_id,
            status=outcome.status,
            finished_at=finished,
            duration=finished - started,
            exit_code=outcome.exit_code,
            error=outcome.error[:8000],
            steps=outcome.steps,
            vars=outcome.variables,
            truncated=1 if outcome.truncated else 0,
        )
        if job.get("id"):
            self.db.execute(
                "UPDATE jobs SET last_run = ?, last_status = ?, last_run_id = ? WHERE id = ?",
                (finished, outcome.status, run_id, job["id"]),
            )

        bus.publish(f"run:{run_id}", {
            "type": "finished", "run_id": run_id, "status": outcome.status,
            "duration": round(finished - started, 3), "exit_code": outcome.exit_code,
        })
        publish_event(
            "run.finished", job_id=job.get("id"), job_name=job.get("name"), run_id=run_id,
            status=outcome.status, duration=round(finished - started, 3),
            exit_code=outcome.exit_code,
        )
        log.info(
            "run #%s %s (%s) in %.2fs", run_id, job.get("name"), outcome.status, finished - started
        )

        if outcome.status != "cancelled":
            await self._notify(job, run_id, outcome, trigger, attempt)
        return outcome

    async def _notify(
        self, job: dict, run_id: int, outcome: RunOutcome, trigger: str, attempt: int
    ) -> None:
        policy = (job.get("notify_on") or "failure").lower()
        failed = outcome.status in FAILURE_STATUSES
        if policy == "never" or (policy == "failure" and not failed):
            return

        run = self.db.get_run(run_id) or {}
        run.setdefault("trigger", trigger)
        run.setdefault("attempt", attempt)
        payload = notify.run_payload(job, run, outcome.steps)

        url = (job.get("notify_url") or "").strip() or self.db.get_setting("notify_url", "")
        try:
            if url:
                await notify.send_webhook(url, payload)
        except Exception:  # noqa: BLE001 - notification failure must not fail the run
            log.exception("webhook notification failed for run %s", run_id)

        settings = self.db.all_settings()
        if settings.get("smtp_host") and settings.get("smtp_to"):
            subject = f"[WebRunner] {job.get('name')} — {outcome.status}"
            try:
                await notify.send_email(settings, subject, notify.summary_text(job, run))
            except Exception as exc:  # noqa: BLE001
                log.warning("e-mail notification failed for run %s: %s", run_id, exc)

    # -------------------------------------------------------- cancellation

    async def cancel_run(self, run_id: int, reason: str = "cancelled by user") -> bool:
        active = self.active.get(run_id)
        if not active:
            return False
        log.info("cancelling run #%s (%s)", run_id, reason)
        for pid in list(active.pids):
            terminate_tree(pid, force=False)
        if active.task and not active.task.done():
            active.task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(active.task), timeout=8.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        for pid in list(active.pids):
            terminate_tree(pid, force=True)
        self.db.finish_run(run_id, error=reason)
        return True

    def status(self) -> dict[str, Any]:
        return {
            "uptime": time.time() - self.started_at,
            "active_runs": [
                {
                    "run_id": a.run_id,
                    "job_id": a.job_id,
                    "job_name": a.job_name,
                    "started_at": a.started_at,
                    "elapsed": time.time() - a.started_at,
                }
                for a in self.active.values()
            ],
            "queued": {str(k): v for k, v in self._queued.items() if v},
        }

    def next_runs(self, limit: int = 8) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT id, name, next_run, trigger_type, cron, interval_seconds, tags "
            "FROM jobs WHERE enabled = 1 AND next_run IS NOT NULL "
            "ORDER BY next_run LIMIT ?",
            (limit,),
        )
        return rows


def humanise_interval(seconds: float) -> str:
    seconds = int(seconds)
    if seconds <= 0:
        return "—"
    parts = []
    for label, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size:
            value, seconds = divmod(seconds, size)
            parts.append(f"{value}{label}")
    return " ".join(parts[:2])


def describe_schedule(job: dict) -> str:
    trigger = (job.get("trigger_type") or "cron").lower()
    if trigger == "cron":
        return job.get("cron") or "—"
    if trigger == "interval":
        return f"every {humanise_interval(job.get('interval_seconds') or 0)}"
    if trigger == "date":
        return f"once at {job.get('run_at') or '—'}"
    return "manual only"


def parse_datetime_local(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def next_within(job: dict, window: timedelta) -> bool:
    next_run = job.get("next_run")
    return bool(next_run and next_run - time.time() <= window.total_seconds())
