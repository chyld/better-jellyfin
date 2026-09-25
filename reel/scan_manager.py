"""Runs scans one at a time on a background thread and tracks their progress."""
import logging
import queue
import threading
from collections.abc import Callable
from pathlib import Path

from .db import connect
from .scanner import ScanCancelled, scan_library

log = logging.getLogger(__name__)

ScanFn = Callable[..., dict]


class ScanManager:
    def __init__(self, db_path: Path, *, workers: int = 4, scan_fn: ScanFn = scan_library):
        self._db_path = db_path
        self._workers = workers
        self._scan_fn = scan_fn
        self._queue: queue.Queue[int | None] = queue.Queue()
        self._lock = threading.Lock()
        self._status: dict[int, dict] = {}
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        # Called with the scan's connection after each successful scan.
        self.after_scan: Callable[[object], None] | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="scanner", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10) -> None:
        """Stop soon: queued scans are dropped, a running one stops at its next
        folder or file (keeping what it has recorded). Blocks until it has, so call
        it from a thread, not the event loop."""
        self._cancel.set()
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
            return dict(self._status[library_id])

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
            finally:
                self._queue.task_done()

    def _scan_one(self, library_id: int) -> None:
        with self._lock:
            if library_id not in self._status:
                return  # library was deleted while queued
        self._update(library_id, state="scanning")
        conn = connect(self._db_path)
        try:
            result = self._scan_fn(
                conn,
                library_id,
                workers=self._workers,
                on_progress=lambda done, total: self._update(library_id, done=done, total=total),
                cancel=self._cancel,
            )
            if self.after_scan:
                self.after_scan(conn)
            self._update(library_id, state="done", result=result)
        except ScanCancelled:
            log.info("scan of library %s stopped", library_id)
            self._update(library_id, state="cancelled")
            conn.rollback()
        except Exception as exc:
            log.exception("scan of library %s failed", library_id)
            message = str(exc) or type(exc).__name__
            self._update(library_id, state="error", error=message)
            conn.rollback()
            conn.execute(
                "UPDATE libraries SET last_scan_error = ? WHERE id = ?", (message, library_id)
            )
            conn.commit()
        finally:
            conn.close()
