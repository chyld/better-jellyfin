"""English subtitles for a video, made on this machine by Whisper.

Whisper (faster-whisper, on the CPU) listens to the video's audio and writes
what's said in English, translating on the way (Japanese, or whatever it hears;
English is just transcribed). The result is a WebVTT file, the format browsers
show, kept in the data folder (never on the NAS), one file per version:

    subtitles/<video uuid>-<version>.en.vtt

It runs in a process of its own (subtitle_worker.py) at low priority, one video
at a time (see jobs.py), and is paused (SIGSTOP) while someone is watching, so
it never takes the CPU from playback. The first run downloads the model into
the data folder (models/; large-v3 is about 3 GB).

Subtitles belong to the video's catalog row: they follow a moved or renamed
file, and go when the video is removed from the catalog. A replaced file keeps
them (it's most likely the same video); make them again if it isn't.
"""
import asyncio
import json
import os
import signal
import sqlite3
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import anyio

from .catalog import NotFound
from .db import connect, new_uid, write_transaction
from .images import ORPHAN_GRACE_SECONDS
from .jobs import JobError, JobQueue
from .playback import drain

LANGUAGE = "en"          # what subtitles are made in: Whisper only translates into English
WATCH_CHECK_SECONDS = 2.0

# Lines Whisper is known to invent over silence or music (from the videos it
# learned on): dropped when they're a whole line on their own.
HALLUCINATIONS = {
    "thank you for watching", "thanks for watching", "thank you for watching!", "please subscribe",
    "please subscribe to my channel", "subtitles by the amara.org community", "thank you.",
}


class SubtitleError(JobError):
    """Subtitles couldn't be made; the message says why."""


# ---- Timing and the file (pure) ----------------------------------------------------------


def cues(lines, *, shortest=1.0, per_char=0.08, extra=1.0, longest=7.0) -> list[tuple[float, float, str]]:
    """Subtitle cues from Whisper's lines [(start, end, text)], timed for reading.

    Whisper's times can stretch a short line over a long silence (it's still on
    screen half a minute later), or flash one for a moment. So a line shows for
    about as long as it takes to read (`extra` + `per_char` per character, at most
    `longest`), at least `shortest`, and never over the next one. Empty lines and
    known inventions (HALLUCINATIONS) are dropped."""
    kept = []
    for start, end, text in lines:
        text = " ".join(text.split())
        if not text or text.lower().strip(" .!") in {h.strip(" .!") for h in HALLUCINATIONS}:
            continue
        end = min(end, start + min(longest, extra + per_char * len(text)))
        kept.append([start, max(end, start + shortest), text])
    kept.sort(key=lambda c: c[0])
    for cue, after in zip(kept, kept[1:]):
        if cue[1] > after[0]:
            cue[1] = max(after[0], cue[0] + 0.1)
    return [(round(s, 3), round(e, 3), t) for s, e, t in kept]


def timestamp(seconds: float) -> str:
    """WebVTT time: "01:02:03.450"."""
    ms = round(max(0.0, seconds) * 1000)
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    secs, ms = divmod(ms, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02}.{ms:03}"


def to_vtt(cue_list) -> str:
    """A WebVTT file of these cues."""
    parts = ["WEBVTT", ""]
    for start, end, text in cue_list:
        parts += [f"{timestamp(start)} --> {timestamp(end)}", text.replace("-->", "->"), ""]
    return "\n".join(parts)


# ---- The catalog --------------------------------------------------------------------------


def file_name(item_uid: str, version: str, language: str = LANGUAGE) -> str:
    return f"{item_uid}-{version}.{language}.vtt"


def can_make(row) -> bool:
    """Subtitles need something to listen to: an audio track, and a video that's there."""
    return bool(row["audio_codec"]) and not row["missing_since"] and not row["probe_error"] and bool(row["duration"])


def _video(conn: sqlite3.Connection, item_uid: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM media_items WHERE uid = ?", (item_uid,)).fetchone()
    if row is None:
        raise NotFound("Video not found.")
    return row


def info(conn: sqlite3.Connection, item_id: int, language: str = LANGUAGE) -> dict | None:
    """The video's subtitles in `language`, if it has them."""
    row = conn.execute("SELECT version, source_language, model, made_at FROM subtitles "
                       "WHERE item_id = ? AND language = ?", (item_id, language)).fetchone()
    return dict(row) if row else None


def current_file(conn: sqlite3.Connection, folder: Path, item_uid: str, language: str = LANGUAGE) -> Path | None:
    row = conn.execute("SELECT s.version FROM subtitles s JOIN media_items m ON m.id = s.item_id "
                       "WHERE m.uid = ? AND s.language = ?", (item_uid, language)).fetchone()
    return folder / file_name(item_uid, row["version"], language) if row else None


def _retire(path: Path) -> None:
    """Left for prune() to delete after the grace period (a page may be reading it)."""
    try:
        os.utime(path)
    except FileNotFoundError:
        pass


def remove(conn: sqlite3.Connection, folder: Path, item_uid: str, language: str = LANGUAGE) -> None:
    video = _video(conn, item_uid)
    path = current_file(conn, folder, item_uid, language)
    if path is None:
        return
    _retire(path)
    conn.execute("DELETE FROM subtitles WHERE item_id = ? AND language = ?", (video["id"], language))
    conn.commit()


def retire_library(conn: sqlite3.Connection, folder: Path, library_id: int) -> None:
    for uid, version, language in conn.execute(
            "SELECT m.uid, s.version, s.language FROM subtitles s JOIN media_items m ON m.id = s.item_id "
            "WHERE m.library_id = ?", (library_id,)).fetchall():
        _retire(folder / file_name(uid, version, language))


def prune(conn: sqlite3.Connection, folder: Path, *, grace_seconds: float = ORPHAN_GRACE_SECONDS) -> int:
    """Delete subtitle files nothing points at, once older than the grace period."""
    if not folder.is_dir():
        return 0
    keep = {file_name(uid, version, language) for uid, version, language in conn.execute(
        "SELECT m.uid, s.version, s.language FROM subtitles s JOIN media_items m ON m.id = s.item_id")}
    cutoff = time.time() - grace_seconds
    removed = 0
    for path in folder.glob("*.vtt"):
        try:
            if path.name in keep or path.stat().st_mtime > cutoff:
                continue
        except FileNotFoundError:
            continue
        path.unlink(missing_ok=True)
        removed += 1
    return removed


def listing(conn: sqlite3.Connection, jobs: list[tuple[str, dict]]) -> dict:
    """The Subtitles page: jobs (running first, then waiting in order, then failed),
    and every video with subtitles (newest first)."""
    fields = """m.uid, m.title, m.rel_path, m.custom_image, m.poster_path, m.poster_rev, m.duration,
                l.uid AS library_uid, l.name AS library_name"""

    def video(row) -> dict:
        return {"id": row["uid"], "title": row["title"], "rel_path": row["rel_path"],
                "library_id": row["library_uid"], "library_name": row["library_name"],
                "custom_image": row["custom_image"], "has_poster": row["poster_path"] is not None,
                "poster_rev": row["poster_rev"], "duration": row["duration"]}

    uids = [uid for uid, _ in jobs]
    found = {}
    if uids:
        found = {r["uid"]: r for r in conn.execute(
            f"SELECT {fields} FROM media_items m JOIN libraries l ON l.id = m.library_id "
            f"WHERE m.uid IN ({','.join('?' * len(uids))})", uids)}
    waiting, out_jobs = 0, []
    for uid, status in jobs:
        if uid not in found:
            continue
        job = {**video(found[uid]), **status}
        if status["state"] == "queued":
            waiting += 1
            job["place"] = waiting
        out_jobs.append(job)
    made = [{**video(r), "language": r["language"], "source_language": r["source_language"], "model": r["model"],
             "made_at": r["made_at"]}
            for r in conn.execute(f"SELECT {fields}, s.language, s.source_language, s.model, s.made_at "
                                  "FROM subtitles s JOIN media_items m ON m.id = s.item_id "
                                  "JOIN libraries l ON l.id = m.library_id ORDER BY s.made_at DESC, m.title")]
    return {"jobs": out_jobs, "subtitles": made}


# ---- Making them ------------------------------------------------------------------------------


@dataclass
class Job:
    item_uid: str
    src: Path
    duration: float
    language: str | None = None   # what's spoken, if known (else Whisper works it out)
    status: dict = field(default_factory=lambda: {"state": "queued", "progress": 0.0, "stage": None, "error": None})


class SubtitleManager(JobQueue):
    """Makes subtitles one video at a time (see jobs.py) in a worker process
    (subtitle_worker.py), reading its progress; pauses it while `watching()`."""

    what = "Subtitles"

    def __init__(self, db_path: Path, folder: Path, models_dir: Path, *, model: str = "large-v3",
                 threads: int = 4, watching=lambda: False, worker: list[str] | None = None):
        super().__init__()
        self.db_path = db_path
        self.folder = folder
        self.models_dir = models_dir
        self.model = model
        self.threads = threads
        self.watching = watching
        # The worker's command, before its arguments (tests swap in a stand-in).
        self.worker = worker or [sys.executable, "-m", "reel.subtitle_worker"]

    def clean_up_partials(self) -> None:
        for path in self.folder.glob("*.part"):
            path.unlink(missing_ok=True)

    async def shutdown(self) -> None:
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.send_signal(signal.SIGCONT)   # a paused worker must run to die cleanly
            except ProcessLookupError:
                pass
        await super().shutdown()
        self.clean_up_partials()

    def command(self, job: Job, out: Path) -> list[str]:
        cmd = [*self.worker, str(job.src), str(out), "--model", self.model, "--threads", str(self.threads),
               "--models", str(self.models_dir)]
        return cmd + (["--language", job.language] if job.language else [])

    async def _make(self, job: Job) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        self.models_dir.mkdir(parents=True, exist_ok=True)
        version = new_uid()[:8]
        out = self.folder / file_name(job.item_uid, version)
        part = out.with_suffix(".part")
        errors: deque[str] = deque(maxlen=20)
        spoken = None
        try:
            proc = self._proc = await asyncio.create_subprocess_exec(
                *self.command(job, part), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                preexec_fn=lambda: os.nice(10),            # never ahead of playback
            )
            reader = asyncio.create_task(drain(proc.stderr, errors))
            pauser = asyncio.create_task(self._pause_while_watching(proc, job))
            try:
                while line := await proc.stdout.readline():
                    try:
                        news = json.loads(line)
                    except ValueError:
                        continue
                    if "stage" in news:
                        job.status["stage"] = news["stage"]
                    if "progress" in news:
                        job.status["progress"] = min(1.0, float(news["progress"]))
                    if "language" in news:
                        spoken = news["language"]
            finally:
                pauser.cancel()
                with anyio.CancelScope(shield=True):
                    if proc.returncode is None and not self.wanted(job):
                        proc.kill()
                    try:
                        proc.send_signal(signal.SIGCONT)
                    except ProcessLookupError:
                        pass
                    await proc.wait()
                    try:
                        await asyncio.wait_for(reader, 2)
                    except (TimeoutError, asyncio.CancelledError, Exception):
                        reader.cancel()
            if not self.wanted(job):
                return
            if proc.returncode != 0 or not part.exists():
                raise SubtitleError(f"Whisper couldn't make them ({errors[-1] if errors else proc.returncode}).")
            await anyio.to_thread.run_sync(os.replace, part, out)
            await anyio.to_thread.run_sync(self._record, job, version, spoken)
        except BaseException:
            out.unlink(missing_ok=True)
            raise
        finally:
            part.unlink(missing_ok=True)

    async def _pause_while_watching(self, proc, job: Job) -> None:
        """Stop the worker (SIGSTOP) while someone watches; carry on (SIGCONT) after."""
        paused = False
        while proc.returncode is None:
            watching = await anyio.to_thread.run_sync(self.watching)
            if watching != paused:
                try:
                    proc.send_signal(signal.SIGSTOP if watching else signal.SIGCONT)
                except ProcessLookupError:
                    return
                paused = watching
                job.status["state"] = "paused" if paused else "running"
            await asyncio.sleep(WATCH_CHECK_SECONDS)

    def _record(self, job: Job, version: str, spoken: str | None) -> None:
        conn = connect(self.db_path)
        try:
            with write_transaction(conn):
                row = conn.execute("SELECT id, uid FROM media_items WHERE uid = ?", (job.item_uid,)).fetchone()
                if row is None:
                    raise SubtitleError("The video is no longer in the library.")
                old = conn.execute("SELECT version FROM subtitles WHERE item_id = ? AND language = ?",
                                   (row["id"], LANGUAGE)).fetchone()
                if old:
                    _retire(self.folder / file_name(row["uid"], old["version"]))
                conn.execute(
                    """
                    INSERT INTO subtitles (item_id, language, source_language, version, model) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT (item_id, language) DO UPDATE SET source_language = excluded.source_language,
                        version = excluded.version, model = excluded.model, made_at = datetime('now')
                    """,
                    (row["id"], LANGUAGE, spoken, version, self.model),
                )
        finally:
            conn.close()
