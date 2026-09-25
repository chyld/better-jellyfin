"""Small cached JPEG thumbnails, made with ffmpeg.

The previews on the NAS are often multi-megabyte PNGs, far too heavy for a grid,
so every image the browser sees goes through here. Each image is cropped to
the shape it's shown in first, so it fills that shape sharply:

    poster     2:3, at most 480x720   folders, libraries and tags
    landscape  16:9, at most 640x360  videos
"""
import hashlib
import os
import subprocess
import threading
import time
from pathlib import Path

# Crop to the largest centred area of the shape, then shrink (never enlarge).
SHAPES = {
    "poster": "crop='min(iw,ih*2/3)':'min(ih,iw*3/2)',scale='min(480,iw)':-2",
    "landscape": "crop='min(iw,ih*16/9)':'min(ih,iw*9/16)',scale='min(640,iw)':-2",
}
FFMPEG_TIMEOUT = 60


class ThumbnailError(Exception):
    pass


class Thumbnailer:
    def __init__(self, cache_dir: Path, *, max_concurrent: int = 4):
        self.cache_dir = cache_dir
        # Limit how many ffmpeg processes a page full of new thumbnails can start.
        self._slots = threading.Semaphore(max_concurrent)

    def path_for(self, key_src: str, rev: str, shape: str) -> Path:
        """Where the thumbnail of a picture at version `rev` (its size and time, see
        picture_rev) is kept. `key_src` is the picture's path as the catalog knows
        it. Needs no trip to the NAS: a replaced picture has a new version."""
        key = hashlib.sha256(repr((key_src, rev, shape)).encode()).hexdigest()
        return self.cache_dir / key[:2] / f"{key}.jpg"

    def cached(self, key_src: str, rev: str, shape: str = "poster") -> Path | None:
        """The thumbnail, if it's already made (no NAS, no ffmpeg)."""
        out = self.path_for(key_src, rev, shape)
        return out if out.exists() else None

    def _render(self, out: Path, input_args: list[str], shape: str) -> Path:
        if out.exists():
            return out
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp.jpg")
        cmd = [
            "ffmpeg", "-v", "error", "-y", *input_args,
            "-frames:v", "1", "-vf", SHAPES[shape], "-q:v", "4", str(tmp),
        ]
        with self._slots:
            if out.exists():  # another request made it while we waited
                return out
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
            except subprocess.TimeoutExpired:
                tmp.unlink(missing_ok=True)
                raise ThumbnailError("ffmpeg timed out")
        if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            tmp.unlink(missing_ok=True)
            raise ThumbnailError(proc.stderr.strip() or "ffmpeg made no image")
        os.replace(tmp, out)
        return out

    def make(self, src: Path, shape: str = "poster", *, key_src: str | None = None,
             rev: str | None = None) -> Path:
        """A small JPEG of `src` (the real, already checked file), cropped to `shape`
        ("poster" or "landscape"). Without a recorded `rev` (not scanned since
        versions were recorded), it's read from the file now."""
        rev = rev or picture_rev(src)
        if rev is None:
            raise ThumbnailError(f"Can't read {src.name}")
        return self._render(self.path_for(key_src or str(src), rev, shape), ["-i", str(src)], shape)

    def from_image(self, src: Path, shape: str = "poster") -> Path:
        """A small JPEG of `src`, cropped to `shape` (keyed by its path and version)."""
        return self.make(src, shape)

    def prune(self, keep: set[Path], *, grace_seconds: float = 3600) -> int:
        """Delete thumbnails not in `keep` (old versions, pictures that are gone),
        except recent ones: one may have just been made for a request. Returns how many."""
        cutoff = time.time() - grace_seconds
        removed = 0
        for path in self.cache_dir.glob("*/*.jpg"):
            if path in keep:
                continue
            try:
                if path.stat().st_mtime > cutoff:
                    continue
            except FileNotFoundError:
                continue
            path.unlink(missing_ok=True)
            removed += 1
        return removed


def picture_rev(path: str | os.PathLike) -> str | None:
    """A NAS picture's version: its size and modification time. The scan records
    it, and thumbnails are cached under it. None if it can't be read right now."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return f"{st.st_size}-{st.st_mtime_ns}"


# ---- Uploaded images ---------------------------------------------------------------
#
# Every upload gets its own file, "<uuid>-<version>.jpg", which is never
# overwritten. An upload writes its new file, then records the version in the
# database, then deletes the previous version's file. Clean-up only deletes
# unreferenced files older than ORPHAN_GRACE, so a file that's about to be
# recorded can't be removed from under an upload.

ORPHAN_GRACE_SECONDS = 3600


def upload_name(uid: str, version: str) -> str:
    return f"{uid}-{version}.jpg"

UPLOAD_WIDTH = 800
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def image_kind(data: bytes) -> str | None:
    """Recognise an uploaded image by its first bytes: JPEG, PNG, GIF, WebP or BMP."""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:2] == b"BM":
        return "bmp"
    return None


def save_upload(data: bytes, out: Path, *, width: int = UPLOAD_WIDTH) -> None:
    """Store an uploaded image as a JPEG at most `width` pixels wide.

    Raises ThumbnailError if it isn't an image ffmpeg can read. The file is
    replaced atomically, so a failed upload leaves any previous image alone.
    """
    if not image_kind(data):
        raise ThumbnailError("That isn't a JPG, PNG, WebP, GIF or BMP image.")
    out.parent.mkdir(parents=True, exist_ok=True)
    stamp = f"{os.getpid()}.{threading.get_ident()}"
    src = out.with_suffix(f".{stamp}.upload")
    tmp = out.with_suffix(f".{stamp}.tmp.jpg")
    try:
        src.write_bytes(data)
        cmd = [
            "ffmpeg", "-v", "error", "-y", "-i", str(src), "-frames:v", "1",
            "-vf", f"scale='min({width},iw)':-2", "-q:v", "3", str(tmp),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise ThumbnailError("The image took too long to process.")
        if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            raise ThumbnailError("That image couldn't be read. Is the file damaged?")
        os.replace(tmp, out)
    finally:
        src.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)


# ---- Frames from videos ------------------------------------------------------------

FRAME_TIMEOUT = 30


def save_frame(src: Path, seconds: float, out: Path, *, interlaced: bool = False,
               width: int = UPLOAD_WIDTH) -> None:
    """Save one frame of a video, at `seconds`, as a JPEG at most `width` wide, in
    one ffmpeg run (stored like an upload).

    Taken from the original file, so it's full quality whatever the player was
    sent. Interlaced video is deinterlaced, as it is for playback. Replaced
    atomically; raises ThumbnailError if there's no frame to take."""
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y"]
    if seconds > 0:
        cmd += ["-ss", f"{seconds:.3f}"]   # before -i: fast, and exact (decodes up to the frame)
    filters = (["bwdif=mode=send_frame"] if interlaced else []) + [f"scale='min({width},iw)':-2"]
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp.jpg")
    cmd += ["-i", str(src), "-map", "0:V:0", "-frames:v", "1", "-vf", ",".join(filters), "-q:v", "3", str(tmp)]
    try:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=FRAME_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise ThumbnailError("Taking the picture took too long.")
        if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            detail = proc.stderr.strip().splitlines()
            raise ThumbnailError(detail[-1] if detail else "ffmpeg made no picture")
        os.replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)
