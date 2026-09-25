"""Getting videos into the browser.

direct     the file itself, with HTTP range requests (native seeking)
remux      ffmpeg copies the streams into a fragmented MP4, sent as it's made
transcode  ffmpeg re-encodes to H.264/AAC in a fragmented MP4, sent as it's made

Streams made by ffmpeg can't be range-requested, so seeking starts a new stream
at the chosen time (`start`); the player keeps track of the offset.
"""
import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import anyio

log = logging.getLogger(__name__)

CHUNK_SIZE = 64 * 1024

# Content types browsers accept for files served as-is. QuickTime .mov files that
# passed the "direct" check hold H.264/AAC, which browsers play when called MP4.
DIRECT_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/mp4",
    ".webm": "video/webm",
}

# Fragmented MP4 that can be played while it's still being written.
FMP4_OUTPUT = [
    "-movflags", "frag_keyframe+empty_moov+default_base_moof",
    "-f", "mp4", "pipe:1",
]


def direct_content_type(path: str) -> str:
    return DIRECT_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")


def stream_command(
    src: Path,
    mode: str,
    *,
    start: float = 0,
    interlaced: bool = False,
    height: int | None = None,
    audio_codec: str | None = None,
) -> list[str]:
    """The ffmpeg command that streams `src` from `start` seconds."""
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    if start > 0:
        # Before -i: a fast seek that jumps straight to the nearest keyframe.
        cmd += ["-ss", f"{start:.3f}"]
    # "V" (capital) skips cover art, which some files store as their first video stream.
    cmd += ["-i", str(src), "-map", "0:V:0", "-map", "0:a:0?", "-sn", "-dn"]

    if mode == "remux":
        cmd += ["-c", "copy"]
        if audio_codec == "aac":
            # AAC from MPEG-TS files is in ADTS framing, which MP4 can't hold as-is.
            cmd += ["-bsf:a", "aac_adtstoasc"]
        return cmd + FMP4_OUTPUT
    if mode != "transcode":
        raise ValueError(f"can't stream in mode {mode!r}")

    filters = []
    if interlaced:
        filters.append("bwdif=mode=send_frame")
    if height and height > 1080:
        filters.append("scale=-2:1080")
    # H.264 in 4:2:0 needs even dimensions; old codecs sometimes have odd ones.
    filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
    return cmd + [
        "-vf", ",".join(filters),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-pix_fmt", "yuv420p", "-profile:v", "high",
        # A keyframe every 2 seconds keeps fragments small, so playback starts fast.
        "-force_key_frames", "expr:gte(t,n_forced*2)",
        "-c:a", "aac", "-b:a", "160k", "-ac", "2",
        *FMP4_OUTPUT,
    ]


class StreamBusy(Exception):
    """Every stream slot is taken; the viewer should try again shortly."""


class StreamFailed(Exception):
    """ffmpeg couldn't produce the video; the message says why."""


@dataclass
class StreamLimits:
    max_streams: int = 3          # remuxes + conversions running at once
    wait_for_slot: float = 5.0    # a seek frees its old slot quickly, so wait a little
    startup_timeout: float = 30.0 # first bytes must arrive within this
    stall_timeout: float = 60.0   # no new bytes for this long (while the viewer waits) = stuck
    error_lines: int = 50         # how much of ffmpeg's error output to keep


class StreamManager:
    """Runs one ffmpeg per stream, within limits, and always cleans up.

    - At most `max_streams` run at once; others wait briefly, then get StreamBusy.
    - ffmpeg's error output is read continuously (a full, unread pipe would
      block ffmpeg and freeze the stream), keeping only the last lines.
    - No output within `startup_timeout`, or none for `stall_timeout` while the
      viewer is waiting, ends the stream.
    - When the stream ends for any reason, including the viewer leaving, ffmpeg
      is killed and reaped, and its slot is freed. shutdown() ends them all.
    """

    def __init__(self, limits: StreamLimits | None = None):
        self.limits = limits or StreamLimits()
        self._slots = asyncio.Semaphore(self.limits.max_streams)
        self.active: set[asyncio.subprocess.Process] = set()

    async def stream(self, cmd: list[str]) -> AsyncIterator[bytes]:
        """Yield ffmpeg's output as it's made. Raises StreamBusy or StreamFailed
        before the first chunk if it can't start."""
        try:
            await asyncio.wait_for(self._slots.acquire(), self.limits.wait_for_slot)
        except TimeoutError:
            raise StreamBusy(
                f"The server is already converting {self.limits.max_streams} videos. Try again in a moment."
            )
        proc = None
        errors: deque[str] = deque(maxlen=self.limits.error_lines)
        drain = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            self.active.add(proc)
            drain = asyncio.create_task(_drain(proc.stderr, errors))

            try:
                first = await asyncio.wait_for(proc.stdout.read(CHUNK_SIZE), self.limits.startup_timeout)
            except TimeoutError:
                raise StreamFailed(f"ffmpeg produced no video within {self.limits.startup_timeout:g} seconds.")
            if not first:
                await proc.wait()
                await drain
                detail = errors[-1] if errors else f"exit code {proc.returncode}"
                raise StreamFailed(f"ffmpeg couldn't play this video ({detail}).")
            yield first

            while True:
                try:
                    chunk = await asyncio.wait_for(proc.stdout.read(CHUNK_SIZE), self.limits.stall_timeout)
                except TimeoutError:
                    log.warning("ffmpeg stalled for %ss; stopping it. Last output: %s",
                                self.limits.stall_timeout, " | ".join(errors))
                    return
                if not chunk:
                    break
                yield chunk
            await proc.wait()
            if proc.returncode != 0:
                log.warning("ffmpeg exited with %s: %s", proc.returncode, " | ".join(errors))
        finally:
            with anyio.CancelScope(shield=True):
                if proc is not None:
                    if proc.returncode is None:
                        proc.kill()
                        await proc.wait()
                    # Only once it's really gone, so `active` never under-reports.
                    self.active.discard(proc)
                if drain is not None:
                    drain.cancel()
                    try:
                        await drain
                    except (asyncio.CancelledError, Exception):
                        pass
            self._slots.release()

    async def shutdown(self) -> None:
        """Stop every running stream (server shutdown)."""
        procs = list(self.active)
        for proc in procs:
            if proc.returncode is None:
                proc.kill()
        for proc in procs:
            await proc.wait()


MAX_LINE = 500


async def _drain(pipe: asyncio.StreamReader, keep: deque[str]) -> None:
    """Read a pipe to the end, keeping its last lines (each capped in length).

    Reads in chunks rather than lines: readline() gives up on very long lines,
    and a reader that stops reading would let the pipe fill and block ffmpeg.
    """
    pending = b""
    while chunk := await pipe.read(CHUNK_SIZE):
        pending += chunk
        *lines, pending = pending.split(b"\n")
        for line in lines:
            keep.append(line.decode(errors="replace").rstrip()[:MAX_LINE])
        if len(pending) > MAX_LINE:  # an endless line: keep its start, drop the rest
            keep.append(pending[:MAX_LINE].decode(errors="replace"))
            pending = b""
    if pending:
        keep.append(pending.decode(errors="replace").rstrip()[:MAX_LINE])
