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

from .plan import Plan

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


def input_args(src: Path, start: float) -> list[str]:
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    if start > 0:
        # Before -i: a fast seek that jumps straight to the nearest keyframe.
        cmd += ["-ss", f"{start:.3f}"]
    # "V" (capital) skips cover art, which some files store as their first video stream.
    return cmd + ["-i", str(src), "-map", "0:V:0", "-map", "0:a:0?", "-sn", "-dn"]


def video_args(plan: Plan, *, interlaced: bool, height: int | None, keyframe_every: float) -> list[str]:
    if plan.video == "copy":
        return ["-c:v", "copy"]
    filters = []
    if interlaced:
        filters.append("bwdif=mode=send_frame")
    if height and height > 1080:
        filters.append("scale=-2:1080")
    # H.264 in 4:2:0 needs even dimensions; old codecs sometimes have odd ones.
    filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
    return [
        "-vf", ",".join(filters),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-pix_fmt", "yuv420p", "-profile:v", "high",
        # Regular keyframes: small fragments start fast, and HLS segments cut exactly.
        "-force_key_frames", f"expr:gte(t,n_forced*{keyframe_every:g})",
    ]


def audio_args(plan: Plan, audio_codec: str | None, *, container: str = "mp4") -> list[str]:
    if plan.audio == "copy":
        args = ["-c:a", "copy"]
        if audio_codec == "aac" and container == "mp4":
            # AAC from MPEG-TS files is in ADTS framing, which MP4 can't hold as-is.
            args += ["-bsf:a", "aac_adtstoasc"]
        return args
    if plan.audio == "encode":
        return ["-c:a", "aac", "-b:a", "160k", "-ac", "2"]
    return []


def stream_command(
    src: Path,
    plan: Plan,
    *,
    start: float = 0,
    interlaced: bool = False,
    height: int | None = None,
    audio_codec: str | None = None,
) -> list[str]:
    """The ffmpeg command that streams `src` from `start` seconds, following `plan`:
    the video and the audio are each copied untouched or converted."""
    if not plan.streamed:
        raise ValueError(f"a {plan.mode!r} plan isn't streamed")
    output = FMP4_OUTPUT
    if plan.audio == "copy" and audio_codec in ("ac3", "eac3"):
        # ffmpeg needs the first (E-)AC-3 packet before it can write the MP4 header.
        output = [x + "+delay_moov" if x.startswith("frag_keyframe") else x for x in FMP4_OUTPUT]
    return [
        *input_args(src, start),
        *video_args(plan, interlaced=interlaced, height=height, keyframe_every=2),
        *audio_args(plan, audio_codec),
        *output,
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

    async def acquire_slot(self) -> None:
        """Take one of the `max_streams` slots (HLS encoders use these too)."""
        try:
            await asyncio.wait_for(self._slots.acquire(), self.limits.wait_for_slot)
        except TimeoutError:
            raise StreamBusy(
                f"The server is already converting {self.limits.max_streams} videos. Try again in a moment."
            )

    def release_slot(self) -> None:
        self._slots.release()

    async def stream(self, cmd: list[str]) -> AsyncIterator[bytes]:
        """Yield ffmpeg's output as it's made. Raises StreamBusy or StreamFailed
        before the first chunk if it can't start."""
        await self.acquire_slot()
        proc = None
        errors: deque[str] = deque(maxlen=self.limits.error_lines)
        reader = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            self.active.add(proc)
            reader = asyncio.create_task(drain(proc.stderr, errors))

            try:
                first = await asyncio.wait_for(proc.stdout.read(CHUNK_SIZE), self.limits.startup_timeout)
            except TimeoutError:
                raise StreamFailed(f"ffmpeg produced no video within {self.limits.startup_timeout:g} seconds.")
            if not first:
                await proc.wait()
                await reader
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
                if reader is not None:
                    reader.cancel()
                    try:
                        await reader
                    except (asyncio.CancelledError, Exception):
                        pass
            self.release_slot()

    async def shutdown(self) -> None:
        """Stop every running stream (server shutdown)."""
        procs = list(self.active)
        for proc in procs:
            if proc.returncode is None:
                proc.kill()
        for proc in procs:
            await proc.wait()


MAX_LINE = 500


async def drain(pipe: asyncio.StreamReader, keep: deque[str]) -> None:
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
