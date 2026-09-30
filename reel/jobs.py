"""A queue of background jobs, one at a time on the event loop: MP4 copies
(copies.py) and subtitles (subtitles.py).

A job is anything with an `item_uid` (the video it's for) and a `status` dict:
{"state": "queued" | "running" | "paused" | "error", "progress": 0..1, "error": ...}.
It's kept in memory while it's queued or running, and after a failure (with the
reason); a finished job is in the catalog instead. The queue is lost when Reel
stops: nothing half-made is kept, and the job is simply asked for again.
"""
import asyncio
import logging

log = logging.getLogger(__name__)


class JobError(Exception):
    """A job couldn't be done; the message says why."""


class JobQueue:
    what = "job"   # for the log: "MP4 copy", "subtitles"

    def __init__(self):
        self.jobs: dict[str, object] = {}
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None   # the job's process, if it runs one
        self._current = None

    def start(self) -> None:
        self._worker = asyncio.create_task(self._run())

    def status(self, item_uid: str) -> dict | None:
        job = self.jobs.get(item_uid)
        return dict(job.status) if job else None

    def all(self) -> list[tuple[str, dict]]:
        """Every job there is (item uid, status): the running one, then the queued ones
        in order, then failures."""
        order = {"running": 0, "paused": 0, "queued": 1, "error": 2}
        jobs = [(uid, dict(job.status)) for uid, job in list(self.jobs.items())]
        return sorted(jobs, key=lambda j: order.get(j[1]["state"], 3))   # stable: queue order kept

    def request(self, job) -> dict:
        """Queue a job (on the event loop). Asking again while one is queued or
        running does nothing."""
        current = self.jobs.get(job.item_uid)
        if current and current.status["state"] in ("queued", "running", "paused"):
            return dict(current.status)
        self.jobs[job.item_uid] = job
        self._queue.put_nowait(job)
        return dict(job.status)

    def wanted(self, job) -> bool:
        """Is the job still asked for (not cancelled)?"""
        return self.jobs.get(job.item_uid) is job

    def cancel(self, item_uid: str) -> None:
        """Drop the video's queued, running or failed job (on the event loop)."""
        job = self.jobs.pop(item_uid, None)
        if job is not None and job is self._current and self._proc and self._proc.returncode is None:
            self._proc.kill()

    async def shutdown(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.kill()
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while True:
            job = await self._queue.get()
            if not self.wanted(job):
                continue                        # cancelled while queued
            self._current = job
            job.status["state"] = "running"
            try:
                await self._make(job)
                if self.wanted(job):
                    del self.jobs[job.item_uid]  # done: the catalog has it now
            except JobError as exc:
                job.status.update(state="error", error=str(exc))
                log.warning("%s of %s failed: %s", self.what, job.item_uid, exc)
            except Exception:
                log.exception("%s of %s failed unexpectedly", self.what, job.item_uid)
                job.status.update(state="error", error="It failed unexpectedly; see the log.")
            finally:
                self._current = None
                self._proc = None

    async def _make(self, job) -> None:
        raise NotImplementedError
