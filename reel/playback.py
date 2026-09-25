"""Getting videos into the browser.

direct     the file itself, with HTTP range requests (native seeking)
remux      ffmpeg copies the streams into a fragmented MP4, sent as it's made
transcode  ffmpeg re-encodes to H.264/AAC in a fragmented MP4, sent as it's made

Streams made by ffmpeg can't be range-requested, so seeking starts a new stream
at the chosen time (`start`); the player keeps track of the offset.
"""
import asyncio
import logging
from collections.abc import AsyncIterator
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

# ffmpeg streams in flight, so tests (and later an admin page) can see them.
active_streams: set[asyncio.subprocess.Process] = set()


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


async def ffmpeg_stream(cmd: list[str]) -> AsyncIterator[bytes]:
    """Yield ffmpeg's output as it's produced; kill ffmpeg when the viewer leaves.

    When the browser disconnects (closed tab, or a seek that starts a new
    stream), the response stops iterating and the `finally` block ends ffmpeg,
    so no orphaned encoders are left running.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    active_streams.add(proc)
    try:
        while chunk := await proc.stdout.read(CHUNK_SIZE):
            yield chunk
        await proc.wait()
        if proc.returncode != 0:
            err = (await proc.stderr.read()).decode(errors="replace").strip()
            log.warning("ffmpeg exited with %s: %s", proc.returncode, err[-500:])
    finally:
        active_streams.discard(proc)
        if proc.returncode is None:
            proc.kill()
            # The server cancels this generator when the viewer disconnects;
            # shield the wait so the killed process is always reaped.
            with anyio.CancelScope(shield=True):
                await proc.wait()
