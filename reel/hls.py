"""HLS for converted videos: a full playlist up front, segments encoded on demand.

The playlist lists every segment of the whole video (fixed 6-second segments),
so the player knows the length and can seek anywhere. Segments are encoded by
ffmpeg into a cache folder per *session* (one video, one plan, one version of
the file), with a keyframe forced at every segment boundary so segments cut
exactly where the playlist says.

Each playlist load is a *viewer* (a player in a tab), with its own position and
at most one encoder of its own. Viewers of the same session share the cached
segments, but never restart each other's encoder, so two tabs at different
points of a video don't fight. For a viewer's request:

- a segment that's already encoded is served at once (seeking back is free);
- one just ahead of any running encoder is waited for;
- one far away (a big seek) restarts *this viewer's* encoder at that segment,
  with timestamps offset so it lines up with the playlist;
- an encoder stops once it's AHEAD_LIMIT segments past the viewers it serves,
  and a later request resumes it;
- an encoder that produces nothing for STARTUP_TIMEOUT / STALL_TIMEOUT, or
  that its viewer gave up waiting for, is killed and reaped, and the reason is
  kept for the error message.

Each encoder writes into a private staging folder; finished segments are then
published into the session's shared folder with a hard link, which fails if the
segment is already there. So when two viewers' encoders overlap, the first
finished copy of a segment wins and nothing ever writes over a published one.

Sessions are keyed on the file's size and modification time, the facts the
encoder uses (length, height, interlacing, audio codec) and PROFILE_VERSION, so
a replaced file, a re-probe that changed those facts, or changed encoder
settings never serve old segments: the old session's encoders are stopped, then
its cache is removed. A moved file (same contents) keeps its session, with the
new path.

Viewers unused for IDLE_SECONDS are dropped, and then sessions without viewers.
The caller recreates a dropped session from the segment URL (see main.py), so a
long pause just resumes. The cache has a size target (REEL_HLS_CACHE_MB): over
it, the segments no viewer is near are deleted first. It's a soft target: the
segments viewers are about to play (at most about KEEP_NEAR per viewer) and the
ones being encoded are kept even over it, and a warning is logged.

Encoders take the same slots as other streams (REEL_MAX_STREAMS).

Segments are MPEG-TS. Fragmented MP4 segments were tried first, but ffmpeg's
HLS muxer restarts their timestamps at zero on every run (the offset only goes
into the init segment's edit list), so segments from a restarted encoder landed
at the start of the timeline. TS stamps every packet with its real time.
"""
import asyncio
import hashlib
import itertools
import logging
import math
import os
import re
import secrets
import shutil
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import anyio

from .plan import Plan
from .playback import StreamManager, drain, input_args, video_args, audio_args

log = logging.getLogger(__name__)

# Bump when the encoder's output changes (arguments, segment length, ...), so
# segments made the old way are never mixed with new ones.
PROFILE_VERSION = 1

SEGMENT = 6.0           # seconds per segment
LOOKAHEAD = 4           # a request up to this far past an encoder waits for it
AHEAD_LIMIT = 20        # stop encoding this many segments past the viewers (2 minutes)
KEEP_BEHIND = 30        # delete segments further than this behind every viewer
IDLE_SECONDS = 600      # drop a viewer (and then its session) unused for this long
SEGMENT_TIMEOUT = 60.0  # a viewer gives up waiting for a segment after this long
STARTUP_TIMEOUT = 60.0  # an encoder must finish its first segment within this
STALL_TIMEOUT = 60.0    # ...and each next one within this
MAX_VIEWERS = 8         # per session; the least recently used is dropped beyond this
KEEP_NEAR = AHEAD_LIMIT + LOOKAHEAD + 2  # segments around a viewer the cache limit never deletes
CACHE_LIMIT = 2048 * 1024**2  # bytes, all sessions together (REEL_HLS_CACHE_MB)
POLL = 0.1

_VIEWER = re.compile(r"[0-9a-f]{16}")


class HlsError(Exception):
    """A segment couldn't be produced; the message says why."""


class HlsGone(HlsError):
    """The session's file has changed: the player must load a new playlist."""


def segment_count(duration: float) -> int:
    return max(1, math.ceil(duration / SEGMENT - 1e-6))


def new_viewer() -> str:
    return secrets.token_hex(8)


def valid_viewer(viewer: str) -> bool:
    return bool(_VIEWER.fullmatch(viewer))


def playlist(duration: float, session: str, viewer: str, query: str = "") -> str:
    """The whole video as a VOD playlist of fixed segments.

    Segment URLs carry the session, the viewer and `query` (the browser's
    capabilities), so an expired session can be recreated from them alone."""
    count = segment_count(duration)
    suffix = f"?{query}" if query else ""
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
        lines += [f"#EXTINF:{max(length, 0.001):.3f},", f"hls/{session}/{viewer}/{n}.ts{suffix}"]
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def revision(path: Path) -> str:
    """Which version of the file this is: changes when it's replaced or edited."""
    st = path.stat()
    return f"{st.st_size}-{st.st_mtime_ns}"


def session_id(item_uid: str, source: "Source") -> str:
    """Stable per video, plan, file version, encoder facts and encoder profile."""
    key = f"{item_uid}|{source.plan.video}|{source.plan.audio}|{source.version}|{PROFILE_VERSION}"
    return hashlib.sha256(key.encode()).hexdigest()[:20]


@dataclass
class Source:
    """What an encoder needs to know about the video."""
    path: Path
    plan: Plan
    duration: float
    interlaced: bool
    height: int | None
    audio_codec: str | None
    revision: str

    @property
    def version(self) -> tuple:
        """Everything about the file that changes the segments (not its path)."""
        return (self.revision, round(self.duration, 3), self.interlaced, self.height, self.audio_codec)


@dataclass
class Encoder:
    """One ffmpeg run, encoding from segment `start` onwards for a viewer."""
    owner: str
    start: int
    proc: asyncio.subprocess.Process
    staging: Path                # where this encoder writes; see HlsManager.publish
    progress_at: float = field(default_factory=time.monotonic)
    highest: int = -1            # the highest segment known to exist from `start` on
    errors: deque = field(default_factory=lambda: deque(maxlen=20))
    failure: str | None = None   # why it was stopped, if it failed
    watcher: asyncio.Task | None = None

    def running(self) -> bool:
        return self.proc.returncode is None


@dataclass
class Viewer:
    id: str
    position: int = 0
    last_used: float = field(default_factory=time.monotonic)
    encoder: Encoder | None = None
    last_failure: str | None = None


@dataclass
class Session:
    sid: str
    item_uid: str
    source: Source
    folder: Path
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    viewers: dict[str, Viewer] = field(default_factory=dict)
    last_used: float = field(default_factory=time.monotonic)
    starts: int = 0              # encoders started (for tests and logs)
    retired: bool = False

    @property
    def count(self) -> int:
        return segment_count(self.source.duration)

    def segment(self, n: int) -> Path:
        return self.folder / f"{n}.ts"

    def encoders(self) -> list[Encoder]:
        return [v.encoder for v in self.viewers.values() if v.encoder is not None]

    def produced(self, enc: Encoder) -> int:
        """The highest segment available from the encoder's start on, without a gap
        (start - 1 if none). Remembered, since segments behind viewers get deleted."""
        n = max(enc.highest, enc.start - 1)
        while self.segment(n + 1).exists():
            n += 1
        if n > enc.highest:
            enc.highest = n
            enc.progress_at = time.monotonic()
        return n

    def covering(self, n: int) -> Encoder | None:
        """A running encoder that will soon produce segment n."""
        for enc in self.encoders():
            if enc.running() and enc.start <= n <= self.produced(enc) + LOOKAHEAD:
                return enc
        return None


class HlsManager:
    def __init__(self, cache_dir: Path, streams: StreamManager, cache_limit: int = CACHE_LIMIT):
        self.cache_dir = cache_dir
        self.streams = streams
        self.cache_limit = cache_limit
        self.sessions: dict[str, Session] = {}
        self._runs = itertools.count(1)
        self._over_limit = False
        self._cache_bytes = 0     # as of the last housekeeping pass

    def start(self) -> None:
        """Leftovers from a previous run are useless without their sessions: start clean.
        Call once this process owns the data folder."""
        shutil.rmtree(self.cache_dir, ignore_errors=True)

    async def open(self, item_uid: str, source: Source) -> str:
        """Register (or refresh) the session for this video, plan and file version.

        Sessions of the same video made from another version of the file are
        retired. A session whose file has moved carries on from the new path."""
        sid = session_id(item_uid, source)
        for other in [s for s in self.sessions.values() if s.item_uid == item_uid and s.sid != sid]:
            if other.source.version != source.version:   # made from other facts: stale
                await self.retire(other)
        session = self.sessions.get(sid)
        if session is None:
            folder = self.cache_dir / sid
            folder.mkdir(parents=True, exist_ok=True)
            self.sessions[sid] = session = Session(sid, item_uid, source, folder)
        elif session.source.path != source.path:
            async with session.lock:
                for viewer in session.viewers.values():
                    await self._stop(viewer)
                session.source = source
        session.last_used = time.monotonic()
        return sid

    def get(self, sid: str) -> Session | None:
        return self.sessions.get(sid)

    async def retire(self, session: Session) -> None:
        """Stop the session's encoders, then delete its segments."""
        session.retired = True
        if self.sessions.get(session.sid) is session:
            del self.sessions[session.sid]
        async with session.lock:
            for viewer in session.viewers.values():
                await self._stop(viewer)
        shutil.rmtree(session.folder, ignore_errors=True)

    # ---- serving -----------------------------------------------------------------

    async def media_segment(self, session: Session, viewer_id: str, n: int) -> Path:
        if not 0 <= n < session.count:
            raise HlsError("No such segment.")
        if not valid_viewer(viewer_id):
            raise HlsError("Unknown player.")
        viewer = await self._viewer(session, viewer_id)
        now = time.monotonic()
        viewer.position, viewer.last_used, session.last_used = n, now, now
        path = session.segment(n)
        started = False
        deadline = now + SEGMENT_TIMEOUT
        while not path.exists():
            self.publish(session)
            if path.exists():
                break
            if session.retired:
                raise HlsGone("The video has changed. Reload the player.")
            if session.covering(n) is None:
                async with session.lock:
                    if not path.exists() and session.covering(n) is None and not session.retired:
                        if started:  # our encoder has ended
                            enc = viewer.encoder
                            if enc is not None and enc.watcher is not None:
                                await asyncio.wait({enc.watcher})  # it publishes its last segments
                            self.publish(session)
                            if path.exists():
                                continue                          # it made it after all
                            raise HlsError(self._failure(viewer))  # it didn't: the watcher says why
                        await self._start(session, viewer, n)
                        started = True
            if time.monotonic() > deadline:
                async with session.lock:
                    await self._stop(viewer, failure="a viewer gave up waiting for it")
                raise HlsError("Timed out waiting for the video.")
            await asyncio.sleep(POLL)
        self._trim(session)
        return path

    @staticmethod
    def _failure(viewer: Viewer) -> str:
        detail = viewer.last_failure or "it stopped"
        return f"ffmpeg couldn't produce this part of the video ({detail})."

    async def _viewer(self, session: Session, viewer_id: str) -> Viewer:
        viewer = session.viewers.get(viewer_id)
        if viewer is None:
            async with session.lock:
                while viewer_id not in session.viewers and len(session.viewers) >= MAX_VIEWERS:
                    oldest = min(session.viewers.values(), key=lambda v: v.last_used)
                    await self._stop(oldest)
                    del session.viewers[oldest.id]
                viewer = session.viewers.setdefault(viewer_id, Viewer(viewer_id))
        return viewer

    def _trim(self, session: Session) -> None:
        """Delete segments far behind every viewer."""
        if not session.viewers:
            return
        behind = min(v.position for v in session.viewers.values()) - KEEP_BEHIND
        for path in session.folder.glob("*.ts"):
            try:
                if int(path.stem) < behind:
                    path.unlink(missing_ok=True)
            except ValueError:
                continue

    # ---- encoders ------------------------------------------------------------------

    # ---- publishing ------------------------------------------------------------------

    @staticmethod
    def publish(session: Session) -> int:
        """Move finished segments from the encoders' staging folders into the shared
        folder. A hard link is atomic and fails if the segment is already there, so
        the first finished copy wins and a published segment is never written over.
        Returns how many were published."""
        published = 0
        for staging in session.folder.glob("enc-*"):
            try:
                names = [e.name for e in os.scandir(staging) if e.name.endswith(".ts")]  # not .ts.tmp
            except OSError:
                continue
            for name in names:
                src = staging / name
                try:
                    os.link(src, session.folder / name)
                    published += 1
                except FileExistsError:
                    pass                      # another encoder got there first
                except OSError:
                    continue
                src.unlink(missing_ok=True)
        return published

    def command(self, session: Session, start_segment: int, staging: Path) -> list[str]:
        src = session.source
        offset = start_segment * SEGMENT
        return [
            *input_args(src.path, offset),
            *video_args(src.plan, interlaced=src.interlaced, height=src.height, keyframe_every=SEGMENT),
            *audio_args(src.plan, src.audio_codec, container="mpegts"),
            # Timestamps continue from where this segment sits in the playlist.
            "-output_ts_offset", f"{offset:.3f}",
            "-f", "hls", "-hls_time", f"{SEGMENT:g}", "-hls_playlist_type", "vod",
            "-hls_segment_type", "mpegts",
            "-start_number", str(start_segment),
            # Into this encoder's own folder; publish() shares them.
            "-hls_segment_filename", str(staging / "%d.ts"),
            # Segments appear under their final name only once complete.
            "-hls_flags", "independent_segments+temp_file",
            str(staging / "ffmpeg.m3u8"),
        ]

    async def _start(self, session: Session, viewer: Viewer, n: int) -> None:
        """(Re)start the viewer's encoder at segment n. Call with the lock held."""
        await self._stop(viewer)
        # A file replaced since the session began would give segments that don't
        # match the ones already made: retire the session instead.
        try:
            current = await anyio.to_thread.run_sync(revision, session.source.path)
        except OSError:
            current = None
        if current != session.source.revision:
            session.retired = True
            asyncio.create_task(self.retire(session))  # it takes the lock we hold
            raise HlsGone("The video has changed or is missing. Reload the player.")
        await self.streams.acquire_slot()
        staging = session.folder / f"enc-{viewer.id}-{next(self._runs)}"
        try:
            staging.mkdir(parents=True)
            proc = await asyncio.create_subprocess_exec(
                *self.command(session, n, staging), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            self.streams.release_slot()
            raise
        enc = Encoder(viewer.id, n, proc, staging, highest=n - 1)
        viewer.encoder = enc
        viewer.last_failure = None
        session.starts += 1
        self.streams.active.add(proc)
        enc.watcher = asyncio.create_task(self._watch(session, viewer, enc))

    async def _watch(self, session: Session, viewer: Viewer, enc: Encoder) -> None:
        """Drain ffmpeg's errors; stop it when far enough ahead or stuck; always reap
        it, free its slot and keep the reason it failed."""
        proc = enc.proc
        reader = asyncio.create_task(drain(proc.stderr, enc.errors))
        try:
            while proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), 0.25)
                except TimeoutError:
                    pass
                if proc.returncode is not None:
                    break
                self.publish(session)
                produced = session.produced(enc)
                if produced - self._needed(session, enc, produced) >= AHEAD_LIMIT:
                    proc.kill()  # far enough ahead; a later request restarts it
                    break
                limit = STARTUP_TIMEOUT if produced < enc.start else STALL_TIMEOUT
                if time.monotonic() - enc.progress_at > limit:
                    enc.failure = f"no progress for {limit:g} seconds"
                    proc.kill()
                    break
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
                self.publish(session)                              # what it finished last
                shutil.rmtree(enc.staging, ignore_errors=True)     # and any half-written part
                if proc.returncode not in (0, -9) and enc.failure is None:
                    enc.failure = enc.errors[-1] if enc.errors else f"ffmpeg exited with {proc.returncode}"
                if enc.failure:
                    viewer.last_failure = enc.failure
                    log.warning("HLS encoder for %s stopped: %s", session.sid, enc.failure)

    @staticmethod
    def _needed(session: Session, enc: Encoder, produced: int) -> int:
        """The furthest position among the viewers this encoder is serving."""
        positions = [v.position for v in session.viewers.values()
                     if enc.start <= v.position <= produced + LOOKAHEAD]
        owner = session.viewers.get(enc.owner)
        if owner is not None:
            positions.append(owner.position)
        return max(positions, default=produced)

    async def _stop(self, viewer: Viewer, failure: str | None = None) -> None:
        """Kill the viewer's encoder and wait until it's reaped and its slot is free."""
        enc, viewer.encoder = viewer.encoder, None
        if enc is None:
            return
        if enc.running():
            if failure and enc.failure is None:
                enc.failure = failure
            enc.proc.kill()
        if enc.watcher is not None:
            with anyio.CancelScope(shield=True):
                await enc.watcher

    # ---- housekeeping --------------------------------------------------------------

    async def remove_idle(self, idle_seconds: float = IDLE_SECONDS) -> int:
        """Drop viewers unused for a while, then sessions left without viewers.
        Returns how many sessions were removed."""
        cutoff = time.monotonic() - idle_seconds
        removed = 0
        for session in list(self.sessions.values()):
            async with session.lock:
                for viewer in [v for v in session.viewers.values() if v.last_used < cutoff]:
                    await self._stop(viewer)
                    del session.viewers[viewer.id]
            if not session.viewers and session.last_used < cutoff:
                await self.retire(session)
                removed += 1
        return removed

    def cache_size(self) -> int:
        """Everything in the cache now: published segments, and files encoders are
        still writing or haven't published yet. Walks the folder: call it from a
        worker thread (housekeeping does)."""
        return _folder_size(self.cache_dir)

    def status(self) -> dict:
        # The size from the last housekeeping pass (every 15 s): no walk per request.
        return {"sessions": len(self.sessions), "cache_mb": round(self._cache_bytes / 1024**2, 1),
                "target_mb": self.cache_limit // 1024**2}

    async def enforce_cache_limit(self) -> int:
        """Delete segments until the cache fits its size target, least useful first:
        sessions used longest ago, and within them the segments furthest from any
        viewer. Segments a viewer is about to play (KEEP_NEAR around each) are kept.
        Returns bytes freed.

        What to keep is decided here, on the event loop, from the sessions as they
        are now; the file work (walking, stat, delete) runs in a worker thread, so
        a slow disk never holds up playback requests."""
        plan = []
        for session in list(self.sessions.values()):
            wanted = set()
            for v in session.viewers.values():
                wanted.update(range(v.position - 1, v.position - 1 + KEEP_NEAR))
            plan.append((session.folder, session.last_used, wanted, [v.position for v in session.viewers.values()]))
        total, freed = await anyio.to_thread.run_sync(_evict, self.cache_dir, self.cache_limit, plan)
        self._cache_bytes = total - freed
        over = self._cache_bytes > self.cache_limit
        if over and not self._over_limit:
            log.warning("HLS cache is over REEL_HLS_CACHE_MB (%d MB, target %d MB): what's left is "
                        "what viewers are playing now", self._cache_bytes // 1024**2, self.cache_limit // 1024**2)
        self._over_limit = over
        return freed

    async def run_housekeeping(self) -> None:
        while True:
            await asyncio.sleep(15)
            try:
                await self.remove_idle()
                await self.enforce_cache_limit()
            except Exception:
                log.exception("HLS housekeeping failed")

    async def shutdown(self) -> None:
        for session in list(self.sessions.values()):
            await self.retire(session)
        shutil.rmtree(self.cache_dir, ignore_errors=True)


def _folder_size(folder: Path) -> int:
    total = 0
    for root, _, files in os.walk(folder):
        for name in files:
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _evict(cache_dir: Path, limit: int, plan: list) -> tuple[int, int]:
    """In a worker thread: delete published segments no viewer wants until the
    cache fits `limit`. `plan` is (folder, last used, wanted segments, viewer
    positions) per session. Returns (bytes before, bytes freed)."""
    candidates = []
    for folder, last_used, wanted, positions in plan:
        for path in folder.glob("*.ts"):
            try:
                n, size = int(path.stem), path.stat().st_size
            except (ValueError, OSError):
                continue
            if n not in wanted:
                distance = min((abs(n - p) for p in positions), default=math.inf)
                candidates.append((last_used, -distance, path, size))
    total = _folder_size(cache_dir)
    freed = 0
    for _, _, path, size in sorted(candidates, key=lambda c: (c[0], c[1])):
        if total - freed <= limit:
            break
        path.unlink(missing_ok=True)
        freed += size
    return total, freed
