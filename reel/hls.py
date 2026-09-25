"""HLS for converted videos: a full playlist up front, segments encoded on demand.

The playlist lists every segment of the whole video (fixed 6-second segments),
so the player knows the length and can seek anywhere. One ffmpeg per session
encodes segments ahead of the viewer into a small cache folder, with a keyframe
forced at every segment boundary so segments cut exactly where the playlist
says. Then:

- a segment that's already encoded is served at once (seeking back is free);
- one just ahead of the encoder is waited for;
- one far away (a big seek) restarts the encoder at that segment, with
  timestamps offset so it lines up with the playlist;
- the encoder stops once it's AHEAD_LIMIT segments past the viewer, and
  resumes when needed; segments far behind the viewer are deleted;
- a session nobody has asked for in IDLE_SECONDS is stopped and removed.

Encoders take the same slots as other streams (REEL_MAX_STREAMS).

Segments are MPEG-TS. Fragmented MP4 segments were tried first, but ffmpeg's
HLS muxer restarts their timestamps at zero on every run (the offset only goes
into the init segment's edit list), so segments from a restarted encoder landed
at the start of the timeline. TS stamps every packet with its real time.
"""
import asyncio
import hashlib
import logging
import math
import shutil
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import anyio

from .plan import Plan
from .playback import StreamManager, drain, input_args, video_args, audio_args

log = logging.getLogger(__name__)

SEGMENT = 6.0          # seconds per segment
LOOKAHEAD = 4          # a request up to this far past the encoder waits for it
AHEAD_LIMIT = 20       # stop encoding this many segments past the viewer (2 minutes)
KEEP_BEHIND = 30       # delete segments further than this behind the viewer
IDLE_SECONDS = 600     # stop and remove a session nobody has used for this long
SEGMENT_TIMEOUT = 60.0 # give up waiting for a segment after this long
POLL = 0.1


class HlsError(Exception):
    """A segment couldn't be produced; the message says why."""


def segment_count(duration: float) -> int:
    return max(1, math.ceil(duration / SEGMENT - 1e-6))


def playlist(duration: float, session: str) -> str:
    """The whole video as a VOD playlist of fixed segments."""
    count = segment_count(duration)
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{math.ceil(SEGMENT)}",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-INDEPENDENT-SEGMENTS",
    ]
    for n in range(count):
        length = min(SEGMENT, duration - n * SEGMENT) if n == count - 1 else SEGMENT
        lines += [f"#EXTINF:{max(length, 0.001):.3f},", f"hls/{session}/{n}.ts"]
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def session_id(item_uid: str, plan: Plan) -> str:
    """Stable per video and plan, so viewers of the same video share one cache."""
    return hashlib.sha256(f"{item_uid}|{plan.video}|{plan.audio}".encode()).hexdigest()[:20]


@dataclass
class Source:
    """What an encoder needs to know about the video."""
    path: Path
    plan: Plan
    duration: float
    interlaced: bool
    height: int | None
    audio_codec: str | None


@dataclass
class Session:
    sid: str
    source: Source
    folder: Path
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    proc: asyncio.subprocess.Process | None = None
    start: int = 0               # the segment the running encoder started at
    last_request: int = 0
    last_used: float = field(default_factory=time.monotonic)
    errors: deque = field(default_factory=lambda: deque(maxlen=20))
    watcher: asyncio.Task | None = None
    highest: int = -1            # the highest segment the running encoder has finished

    @property
    def count(self) -> int:
        return segment_count(self.source.duration)

    def segment(self, n: int) -> Path:
        return self.folder / f"{n}.ts"

    def produced(self) -> int:
        """The highest segment the running encoder has finished (start - 1 if none).

        Remembered rather than counted from `start`, since segments far behind
        the viewer get deleted."""
        n = max(self.highest, self.start - 1)
        while self.segment(n + 1).exists():
            n += 1
        self.highest = n
        return n

    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None


class HlsManager:
    def __init__(self, cache_dir: Path, streams: StreamManager):
        self.cache_dir = cache_dir
        self.streams = streams
        self.sessions: dict[str, Session] = {}
        # Leftovers from a previous run are useless: start clean.
        shutil.rmtree(cache_dir, ignore_errors=True)

    def open(self, item_uid: str, source: Source) -> str:
        """Register (or refresh) the session for this video and plan; returns its id."""
        sid = session_id(item_uid, source.plan)
        session = self.sessions.get(sid)
        if session is None:
            folder = self.cache_dir / sid
            folder.mkdir(parents=True, exist_ok=True)
            self.sessions[sid] = session = Session(sid, source, folder)
        session.last_used = time.monotonic()
        return sid

    def get(self, sid: str) -> Session | None:
        return self.sessions.get(sid)

    # ---- serving -----------------------------------------------------------------

    async def media_segment(self, session: Session, n: int) -> Path:
        if not 0 <= n < session.count:
            raise HlsError("No such segment.")
        session.last_used = time.monotonic()
        session.last_request = n
        path = session.segment(n)
        if not path.exists():
            async with session.lock:
                if not path.exists():
                    if not session.running() or n < session.start or n > session.produced() + LOOKAHEAD:
                        await self._start(session, n)
        path = await self._wait_for(session, path)
        self._trim(session, n)
        return path

    async def _wait_for(self, session: Session, path: Path) -> Path:
        deadline = time.monotonic() + SEGMENT_TIMEOUT
        while True:
            if path.exists():
                return path
            if not session.running() and not path.exists():
                # One last look: ffmpeg may have finished between the checks.
                await asyncio.sleep(POLL)
                if path.exists():
                    return path
                detail = session.errors[-1] if session.errors else "it stopped"
                raise HlsError(f"ffmpeg couldn't produce this part of the video ({detail}).")
            if time.monotonic() > deadline:
                raise HlsError("Timed out waiting for the video.")
            await asyncio.sleep(POLL)

    def _trim(self, session: Session, n: int) -> None:
        for path in session.folder.glob("*.ts"):
            try:
                index = int(path.stem)
            except ValueError:
                continue
            if index < n - KEEP_BEHIND:
                path.unlink(missing_ok=True)

    # ---- the encoder ---------------------------------------------------------------

    def command(self, session: Session, start_segment: int) -> list[str]:
        src = session.source
        offset = start_segment * SEGMENT
        return [
            *input_args(src.path, offset),
            *video_args(src.plan, interlaced=src.interlaced, height=src.height, keyframe_every=SEGMENT),
            *audio_args(src.plan, src.audio_codec),
            # Timestamps continue from where this segment sits in the playlist.
            "-output_ts_offset", f"{offset:.3f}",
            "-f", "hls", "-hls_time", f"{SEGMENT:g}", "-hls_playlist_type", "vod",
            "-hls_segment_type", "mpegts",
            "-start_number", str(start_segment),
            "-hls_segment_filename", str(session.folder / "%d.ts"),
            # Segments appear under their final name only once complete.
            "-hls_flags", "independent_segments+temp_file",
            str(session.folder / "ffmpeg.m3u8"),
        ]

    async def _start(self, session: Session, n: int) -> None:
        """(Re)start the session's encoder at segment n. Call with the lock held."""
        await self._stop(session)
        await self.streams.acquire_slot()
        try:
            session.errors.clear()
            session.proc = await asyncio.create_subprocess_exec(
                *self.command(session, n), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
        except BaseException:
            self.streams.release_slot()
            raise
        session.start = n
        session.highest = n - 1
        self.streams.active.add(session.proc)
        session.watcher = asyncio.create_task(self._watch(session, session.proc))

    async def _watch(self, session: Session, proc: asyncio.subprocess.Process) -> None:
        """Drain ffmpeg's errors, pause it when far enough ahead, free its slot at the end."""
        reader = asyncio.create_task(drain(proc.stderr, session.errors))
        try:
            while proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), 0.5)
                except TimeoutError:
                    pass
                if proc.returncode is None and session.produced() - session.last_request >= AHEAD_LIMIT:
                    proc.kill()  # far enough ahead; a later request restarts it
            if proc.returncode not in (0, -9) and session.errors:
                log.warning("HLS encoder for %s exited with %s: %s", session.sid, proc.returncode,
                            " | ".join(session.errors))
        finally:
            with anyio.CancelScope(shield=True):
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
                try:
                    await asyncio.wait_for(reader, 2)
                except (TimeoutError, asyncio.CancelledError, Exception):
                    reader.cancel()
                self.streams.active.discard(proc)
                self.streams.release_slot()

    async def _stop(self, session: Session) -> None:
        proc, watcher = session.proc, session.watcher
        session.proc = session.watcher = None
        if proc is not None and proc.returncode is None:
            proc.kill()
        if watcher is not None:
            with anyio.CancelScope(shield=True):
                await watcher

    # ---- housekeeping --------------------------------------------------------------

    async def remove_idle(self, idle_seconds: float = IDLE_SECONDS) -> int:
        """Stop and delete sessions nobody has used recently. Returns how many."""
        cutoff = time.monotonic() - idle_seconds
        stale = [s for s in self.sessions.values() if s.last_used < cutoff]
        for session in stale:
            async with session.lock:
                await self._stop(session)
            shutil.rmtree(session.folder, ignore_errors=True)
            self.sessions.pop(session.sid, None)
        return len(stale)

    async def run_housekeeping(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await self.remove_idle()
            except Exception:
                log.exception("HLS housekeeping failed")

    async def shutdown(self) -> None:
        for session in list(self.sessions.values()):
            await self._stop(session)
        shutil.rmtree(self.cache_dir, ignore_errors=True)
