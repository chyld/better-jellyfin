"""Runs scans one at a time on a background thread and tracks their progress."""
import logging
import queue
import threading
from collections.abc import Callable
from functools import partial
from pathlib import Path

from .db import connect
from .probe import ProbeSupervisor
from .scanner import ScanCancelled, scan_library

log = logging.getLogger(__name__)

ScanFn = Callable[..., dict]


class ScanManager:
    def __init__(self, db_path: Path, *, workers: int = 4, scan_fn: ScanFn | None = None, **scan_options):
        """`scan_fn` replaces the scan (tests); otherwise scan_library runs with this
        manager's own ffprobe supervisor, so stopping it stops only its probes.
        `scan_options` go to scan_library (e.g. missing_grace)."""
        self._db_path = db_path
        self._workers = workers
        self.probes = ProbeSupervisor()
        self._scan_fn = scan_fn or partial(scan_library, probe_fn=self.probes.probe, **scan_options)
        self._queue: queue.Queue[int | None] = queue.Queue()
        self._lock = threading.Lock()
        self._status: dict[int, dict] = {}
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._cancel = threading.Event()
        # Called with the scan's connection after each successful scan.
        self.after_scan: Callable[[object], None] | None = None
        # Called after a scan is done (and shown as done), before the next one
        # starts: optional extra work, e.g. making missing thumbnails. It gets
        # the library and the stop flag, and should stop when that's set.
        self.after_done: Callable[[int, threading.Event], None] | None = None

    def start(self) -> None:
        self._cancel.clear()
        self.probes.allow()
        self._stopping = False
        self._start_thread()

    def _start_thread(self) -> None:
        self._thread = threading.Thread(target=self._run, name="scanner", daemon=True)
        self._thread.start()

    def alive(self) -> bool:
        """Whether the scanner thread is running (for the health check)."""
        return self._thread is not None and self._thread.is_alive()

    def queued(self) -> int:
        return self._queue.qsize()

    def stop(self, timeout: float = 10) -> None:
        """Stop soon: queued scans are dropped, running ffprobes are killed, and a
        running scan stops at its next folder or file (keeping what it has
        recorded). Blocks until it has, so call it from a thread, not the event loop.

        A file-system call stuck on a hung NAS can't be interrupted: it finishes
        when the OS returns it, and the scan writes nothing after it. This stops
        waiting after `timeout`, but the process can't exit before that call
        returns: Python waits for the scan's worker threads when it exits."""
        self._stopping = True
        self._cancel.set()
        self.probes.stop()
        while True:
            try:
                library_id = self._queue.get_nowait()
            except queue.Empty:
                break
            if library_id is not None:
                self._update(library_id, state="cancelled")
            self._queue.task_done()
        if self._thread:
            self._queue.put(None)
            self._thread.join(timeout)
            if self._thread.is_alive():
                log.warning("the scan didn't stop within %s seconds", timeout)
            self._thread = None

    def request(self, library_id: int) -> dict:
        """Queue a scan. Asking again while one is queued or running does nothing."""
        with self._lock:
            current = self._status.get(library_id)
            if current and current["state"] in ("queued", "scanning"):
                return dict(current)
            self._status[library_id] = {"state": "queued", "done": 0, "total": 0}
            self._queue.put(library_id)
            status = dict(self._status[library_id])
        # Each job is contained (see _run), so this shouldn't happen; if the thread
        # died anyway, start a new one rather than queue work nobody will do.
        if self._thread is not None and not self._thread.is_alive() and not self._stopping:
            log.error("the scanner thread had stopped; starting it again")
            self._start_thread()
        return status

    def status(self, library_id: int) -> dict | None:
        with self._lock:
            current = self._status.get(library_id)
            return dict(current) if current else None

    def is_busy(self, library_id: int) -> bool:
        status = self.status(library_id)
        return bool(status) and status["state"] in ("queued", "scanning")

    def forget(self, library_id: int) -> None:
        with self._lock:
            self._status.pop(library_id, None)

    def wait_idle(self, timeout: float = 30) -> None:
        """Block until every queued scan has finished (used by tests)."""
        done = threading.Event()

        def check() -> None:
            self._queue.join()
            done.set()

        threading.Thread(target=check, daemon=True).start()
        if not done.wait(timeout):
            raise TimeoutError("scans still running")

    def _update(self, library_id: int, **fields) -> None:
        with self._lock:
            if library_id in self._status:
                self._status[library_id].update(fields)

    def _run(self) -> None:
        while True:
            library_id = self._queue.get()
            try:
                if library_id is None:
                    return
                self._scan_one(library_id)
            except Exception:
                # Whatever went wrong with this job, the next one still runs.
                log.exception("scanning library %s failed unexpectedly", library_id)
                self._update(library_id, state="error", error="The scan failed unexpectedly; see the log.")
            finally:
                self._queue.task_done()

    def _scan_one(self, library_id: int) -> None:
        with self._lock:
            if library_id not in self._status:
                return  # library was deleted while queued
        self._update(library_id, state="scanning")
        conn = None
        try:
            conn = connect(self._db_path)
            result = self._scan_fn(
                conn,
                library_id,
                workers=self._workers,
                on_progress=lambda done, total: self._update(library_id, done=done, total=total),
                cancel=self._cancel,
            )
            # The scan is saved by now: a failing clean-up afterwards doesn't undo it.
            if self.after_scan:
                try:
                    self.after_scan(conn)
                except Exception:
                    log.exception("clean-up after scanning library %s failed", library_id)
                    conn.rollback()
            self._update(library_id, state="done", result=result)
            if self.after_done:
                try:
                    self.after_done(library_id, self._cancel)
                except Exception:
                    log.exception("follow-up work after scanning library %s failed", library_id)
        except ScanCancelled:
            log.info("scan of library %s stopped", library_id)
            self._update(library_id, state="cancelled")
            if conn is not None:
                conn.rollback()
        except Exception as exc:
            log.exception("scan of library %s failed", library_id)
            message = str(exc) or type(exc).__name__
            self._update(library_id, state="error", error=message)
            self._record_error(conn, library_id, message)
        finally:
            if conn is not None:
                conn.close()

    def _record_error(self, conn, library_id: int, message: str) -> None:
        """Keep the error on the library for later (best effort: the database may be
        what failed; the in-memory status already says it)."""
        try:
            if conn is None:
                conn = connect(self._db_path)
                own = True
            else:
                conn.rollback()
                own = False
            try:
                conn.execute("UPDATE libraries SET last_scan_error = ? WHERE id = ?", (message, library_id))
                conn.commit()
            finally:
                if own:
                    conn.close()
        except Exception:
            log.exception("couldn't record the scan error for library %s", library_id)
