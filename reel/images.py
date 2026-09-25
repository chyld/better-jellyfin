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

    def _cache_path(self, src: Path, *extra) -> Path:
        try:
            st = src.stat()
        except OSError as exc:
            raise ThumbnailError(f"Can't read {src.name}: {exc.strerror}")
        # Keyed on the source's size and mtime, so a replaced file gets a new thumbnail.
        key = hashlib.sha256(
            repr((str(src), st.st_size, st.st_mtime, *extra)).encode()
        ).hexdigest()
        return self.cache_dir / key[:2] / f"{key}.jpg"

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

    def from_image(self, src: Path, shape: str = "poster") -> Path:
        """A small JPEG of `src`, cropped to `shape` ("poster" or "landscape")."""
        return self._render(self._cache_path(src, shape), ["-i", str(src)], shape)


# ---- Uploaded images ---------------------------------------------------------------

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
