"""MP4 copies: a video in the wrong container, copied once into a real MP4.

Some files hold browser-ready tracks in a container the browser can't open,
often an MPEG transport stream saved as ".mp4". Every play of those is
repackaged live by ffmpeg, and every seek restarts it. An MP4 copy fixes that
for good: ffmpeg copies both tracks, untouched, into an MP4 with its index at
the front, saved in the data folder (never on the NAS):

    copies/<video uuid>-<version>.mp4

While the copy is current (made from the video file as the catalog last saw it:
its size and modification time), the video is treated as that MP4: its plan,
its format badge, and the bytes sent all come from the copy, played directly
with native seeking. A file that changes on the NAS (seen by the next scan)
makes the copy stale: the original plays again, and the clean-up deletes it.

Copies are made one at a time, in the background, with progress. Only videos a
copy makes directly playable (in a typical browser) can have one: see can_copy().
"""
import asyncio
import logging
import os
import sqlite3
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import anyio

from .catalog import NotFound, video_rev
from .db import connect, new_uid, write_transaction
from .images import ORPHAN_GRACE_SECONDS, durable_replace
from .plan import plan
from .playback import drain

log = logging.getLogger(__name__)

MP4 = "mov,mp4,m4a,3gp,3g2,mj2"   # ffprobe's name for the MP4 family
DISK_CHECK_SECONDS = 5.0           # how often a running copy checks the free space


class CopyError(Exception):
    """A copy couldn't be made; the message says why."""


def copy_name(item_uid: str, version: str) -> str:
    return f"{item_uid}-{version}.mp4"


def is_current(row) -> bool:
    """Does the video have a copy made from its file as the catalog knows it now?"""
    return bool(row["mp4_copy"]) and row["mp4_copy_of"] == video_rev(row["size"], row["mtime"])


def current_file(folder: Path, row) -> Path | None:
    """The video's current copy (it may still have to be checked for existence)."""
    return folder / copy_name(row["uid"], row["mp4_copy"]) if is_current(row) else None


def as_copied(row) -> dict:
    """The video's facts as an MP4 copy would have them: the same tracks, in MP4."""
    facts = dict(row)
    facts["container"] = MP4
    facts["rel_path"] = str(PurePosixPath(row["rel_path"]).with_suffix(".mp4"))
    return facts


def facts(row):
    """What to plan (and name the format) from: the copy's facts while it's current,
    else the file's own."""
    return as_copied(row) if is_current(row) else row


def can_copy(row) -> bool:
    """A copy is only offered where it helps: the video isn't played directly now,
    and would be as an MP4 (its tracks are browser-ready, just in the wrong box)."""
    if row["missing_since"] or row["probe_error"] or not row["duration"]:
        return False
    return plan(row).mode != "direct" and plan(as_copied(row)).mode == "direct"


def command(src: Path, out: Path, audio_codec: str | None) -> list[str]:
    """ffmpeg: both tracks copied untouched into an MP4 whose index is at the front
    (so it starts playing at once), reporting progress on stdout."""
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(src),
           # "V" (capital) skips cover art stored as a video stream.
           "-map", "0:V:0", "-map", "0:a:0?", "-sn", "-dn", "-c", "copy"]
    if audio_codec == "aac":
        cmd += ["-bsf:a", "aac_adtstoasc"]   # ADTS AAC (as in MPEG-TS) doesn't fit MP4 as-is
    return cmd + ["-movflags", "+faststart", "-f", "mp4", "-progress", "pipe:1", "-nostats", str(out)]


# ---- The catalog ----------------------------------------------------------------------------


def _video(conn: sqlite3.Connection, item_uid: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM media_items WHERE uid = ?", (item_uid,)).fetchone()
    if row is None:
        raise NotFound("Video not found.")
    return row


def remove(conn: sqlite3.Connection, folder: Path, item_uid: str) -> None:
    """Forget the video's copy; its file is retired (see pictures.retire) and deleted
    by prune() once unused for the grace period, so a play already sending it isn't cut off."""
    row = _video(conn, item_uid)
    if not row["mp4_copy"]:
        return
    _retire(folder / copy_name(row["uid"], row["mp4_copy"]))
    conn.execute(f"UPDATE media_items SET {FORGET} WHERE id = ?", (row["id"],))
    conn.commit()


FORGET = "mp4_copy = NULL, mp4_copy_of = NULL, mp4_copy_size = NULL, mp4_copy_at = NULL"


def retire_library(conn: sqlite3.Connection, folder: Path, library_id: int) -> None:
    """Before a library's rows go: retire its copies, like any removed one."""
    for uid, version in conn.execute(
            "SELECT uid, mp4_copy FROM media_items WHERE library_id = ? AND mp4_copy IS NOT NULL",
            (library_id,)).fetchall():
        _retire(folder / copy_name(uid, version))


def _retire(path: Path) -> None:
    try:
        os.utime(path)
    except FileNotFoundError:
        pass


def prune(conn: sqlite3.Connection, folder: Path, *, grace_seconds: float = ORPHAN_GRACE_SECONDS) -> int:
    """Forget stale copies (their video file has changed), and delete copy files
    nothing current points at once they're older than the grace period (one just
    made may not be recorded yet). Returns how many files were deleted."""
    keep = set()
    stale = []
    for row in conn.execute("SELECT id, uid, size, mtime, mp4_copy, mp4_copy_of FROM media_items "
                            "WHERE mp4_copy IS NOT NULL").fetchall():
        if is_current(row):
            keep.add(copy_name(row["uid"], row["mp4_copy"]))
        else:
            stale.append((row["id"],))
    if stale:
        conn.executemany(f"UPDATE media_items SET {FORGET} WHERE id = ?", stale)
        conn.commit()
    if not folder.is_dir():
        return 0
    cutoff = time.time() - grace_seconds
    removed = 0
    for path in folder.glob("*.mp4"):
        if path.name in keep:
            continue
        try:
            if path.stat().st_mtime > cutoff:
                continue
        except FileNotFoundError:
            continue
        path.unlink(missing_ok=True)
        removed += 1
    return removed


def adopt_times(conn: sqlite3.Connection, folder: Path) -> None:
    """Copies made before their time was recorded take their file's time."""
    rows = conn.execute("SELECT id, uid, mp4_copy FROM media_items WHERE mp4_copy IS NOT NULL AND mp4_copy_at IS NULL")
    times = []
    for row in rows.fetchall():
        try:
            mtime = (folder / copy_name(row["uid"], row["mp4_copy"])).stat().st_mtime
        except OSError:
            continue
        times.append((time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(mtime)), row["id"]))
    if times:
        conn.executemany("UPDATE media_items SET mp4_copy_at = ? WHERE id = ?", times)
        conn.commit()


def listing(conn: sqlite3.Connection, jobs: list[tuple[str, dict]]) -> dict:
    """The Copies page: copies being made or waiting (in queue order) and ones that
    failed, then every copy there is (newest first), each with its video."""
    fields = """m.uid, m.title, m.rel_path, m.custom_image, m.poster_path, m.poster_rev, m.size, m.mtime,
                m.mp4_copy, m.mp4_copy_of, m.mp4_copy_size, m.mp4_copy_at, l.uid AS library_uid, l.name AS library_name"""

    def video(row) -> dict:
        return {"id": row["uid"], "title": row["title"], "rel_path": row["rel_path"],
                "library_id": row["library_uid"], "library_name": row["library_name"],
                "custom_image": row["custom_image"], "has_poster": row["poster_path"] is not None,
                "poster_rev": row["poster_rev"], "file_size": row["size"]}

    uids = [uid for uid, _ in jobs]
    found = {}
    if uids:
        found = {r["uid"]: r for r in conn.execute(
            f"SELECT {fields} FROM media_items m JOIN libraries l ON l.id = m.library_id "
            f"WHERE m.uid IN ({','.join('?' * len(uids))})", uids)}
    waiting = 0
    out_jobs = []
    for uid, status in jobs:
        if uid not in found:
            continue
        job = {**video(found[uid]), **status}
        if status["state"] == "queued":
            waiting += 1
            job["place"] = waiting              # 1: next in line
        out_jobs.append(job)
    rows = conn.execute(f"SELECT {fields} FROM media_items m JOIN libraries l ON l.id = m.library_id "
                        "WHERE m.mp4_copy IS NOT NULL ORDER BY m.mp4_copy_at DESC, m.title").fetchall()
    made = [{**video(r), "copy_size": r["mp4_copy_size"], "made_at": r["mp4_copy_at"], "current": is_current(r)}
            for r in rows]
    return {"jobs": out_jobs, "copies": made, "totals": totals(conn)}


def totals(conn: sqlite3.Connection) -> dict:
    """How many copies there are, and the disk they use (for /api/health)."""
    count, size = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(mp4_copy_size), 0) FROM media_items WHERE mp4_copy IS NOT NULL").fetchone()
    return {"count": count, "mb": round(size / 1024**2)}


# ---- Making copies ----------------------------------------------------------------------------


@dataclass
class Job:
    item_uid: str
    src: Path
    duration: float
    audio_codec: str | None
    rev: str                 # the video file's version it's made from (video_rev)
    status: dict = field(default_factory=lambda: {"state": "queued", "progress": 0.0, "error": None})


class CopyManager:
    """Makes copies one at a time on the event loop, reading ffmpeg's progress.

    Status lives in memory while a copy is queued or running, and after a failure
    (with the reason); a finished copy is in the catalog instead. Below the disk's
    free-space reserve (`room()` says no) a running copy is stopped."""

    def __init__(self, db_path: Path, folder: Path, room=lambda: True):
        self.db_path = db_path
        self.folder = folder
        self.room = room
        self.jobs: dict[str, Job] = {}
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._current: Job | None = None

    def clean_up_partials(self) -> None:
        """Copies being made when Reel last stopped are useless: delete them.
        Call once this process owns the data folder."""
        for path in self.folder.glob("*.part"):
            path.unlink(missing_ok=True)

    def start(self) -> None:
        self._worker = asyncio.create_task(self._run())

    def all(self) -> list[tuple[str, dict]]:
        """Every job there is (item uid, status): the running one, then the queued ones
        in order, then failures."""
        order = {"running": 0, "queued": 1, "error": 2}
        jobs = [(uid, dict(job.status)) for uid, job in list(self.jobs.items())]
        return sorted(jobs, key=lambda j: order.get(j[1]["state"], 3))   # stable: queue order kept

    def status(self, item_uid: str) -> dict | None:
        job = self.jobs.get(item_uid)
        return dict(job.status) if job else None

    def request(self, job: Job) -> dict:
        """Queue a copy (on the event loop). Asking again while one is queued or
        running does nothing."""
        current = self.jobs.get(job.item_uid)
        if current and current.status["state"] in ("queued", "running"):
            return dict(current.status)
        self.jobs[job.item_uid] = job
        self._queue.put_nowait(job)
        return dict(job.status)

    def cancel(self, item_uid: str) -> None:
        """Drop the video's queued, running or failed copy (on the event loop)."""
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
        self.clean_up_partials()

    async def _run(self) -> None:
        while True:
            job = await self._queue.get()
            if self.jobs.get(job.item_uid) is not job:
                continue                        # cancelled while queued
            self._current = job
            job.status["state"] = "running"
            try:
                await self._make(job)
                if self.jobs.get(job.item_uid) is job:
                    del self.jobs[job.item_uid]  # done: the catalog has it now
            except CopyError as exc:
                job.status.update(state="error", error=str(exc))
                log.warning("MP4 copy of %s failed: %s", job.item_uid, exc)
            except Exception:
                log.exception("MP4 copy of %s failed unexpectedly", job.item_uid)
                job.status.update(state="error", error="The copy failed unexpectedly; see the log.")
            finally:
                self._current = None
                self._proc = None

    async def _make(self, job: Job) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        version = new_uid()[:8]
        out = self.folder / copy_name(job.item_uid, version)
        part = out.with_suffix(".part")
        errors: deque[str] = deque(maxlen=20)
        try:
            proc = self._proc = await asyncio.create_subprocess_exec(
                *command(job.src, part, job.audio_codec), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            reader = asyncio.create_task(drain(proc.stderr, errors))
            out_of_room = False
            checked = time.monotonic()
            try:
                while line := await proc.stdout.readline():
                    key, _, value = line.decode(errors="replace").strip().partition("=")
                    if key == "out_time_us" and value.isdigit() and job.duration:
                        job.status["progress"] = min(1.0, int(value) / 1e6 / job.duration)
                    if time.monotonic() - checked > DISK_CHECK_SECONDS:
                        checked = time.monotonic()
                        if not await anyio.to_thread.run_sync(self.room):
                            out_of_room = True
                            proc.kill()
                            break
            finally:
                with anyio.CancelScope(shield=True):
                    if proc.returncode is None and (out_of_room or self.jobs.get(job.item_uid) is not job):
                        proc.kill()
                    await proc.wait()
                    try:
                        await asyncio.wait_for(reader, 2)
                    except (TimeoutError, asyncio.CancelledError, Exception):
                        reader.cancel()
            if self.jobs.get(job.item_uid) is not job:
                return                           # cancelled: nothing to keep
            if out_of_room:
                raise CopyError("The server's disk is nearly full, so the copy was stopped.")
            if proc.returncode != 0:
                raise CopyError(f"ffmpeg couldn't copy this video ({errors[-1] if errors else proc.returncode}).")
            job.status["progress"] = 1.0
            await anyio.to_thread.run_sync(durable_replace, part, out)
            await anyio.to_thread.run_sync(self._record, job, version, out)
        except BaseException:
            out.unlink(missing_ok=True)
            raise
        finally:
            part.unlink(missing_ok=True)

    def _record(self, job: Job, version: str, out: Path) -> None:
        """Point the video at its new copy, if it's still the file the copy was made
        from; the previous copy (if any) is retired first, as pictures are."""
        conn = connect(self.db_path)
        try:
            with write_transaction(conn):
                row = conn.execute("SELECT id, uid, size, mtime, mp4_copy FROM media_items WHERE uid = ?",
                                   (job.item_uid,)).fetchone()
                if row is None:
                    raise CopyError("The video is no longer in the library.")
                if video_rev(row["size"], row["mtime"]) != job.rev:
                    raise CopyError("The video file changed while it was being copied. Try again.")
                if row["mp4_copy"]:
                    _retire(self.folder / copy_name(row["uid"], row["mp4_copy"]))
                conn.execute("UPDATE media_items SET mp4_copy = ?, mp4_copy_of = ?, mp4_copy_size = ?, "
                             "mp4_copy_at = datetime('now') WHERE id = ?",
                             (version, job.rev, out.stat().st_size, row["id"]))
        finally:
            conn.close()
