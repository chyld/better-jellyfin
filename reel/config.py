import os
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path


def _choice(name: str, allowed: tuple[str, ...], default: str) -> str:
    value = os.environ.get(name, default).strip().lower()
    if value not in allowed:
        raise SystemExit(f"{name} must be one of: {', '.join(allowed)} (got {value!r}).")
    return value


@dataclass(frozen=True)
class Settings:
    # Libraries must live under this folder; the folder picker can't leave it.
    media_root: Path
    # Where the SQLite database (and later thumbnails/transcodes) are kept.
    data_dir: Path
    # How many ffprobe processes run at once during a scan.
    probe_workers: int = 4
    # How long a video a scan can't find stays (hidden) before it's removed.
    missing_grace: timedelta = timedelta(days=7)
    # Which addresses image URLs may be downloaded from: internet, lan or off.
    image_urls: str = "internet"
    # How many remuxes/conversions may run at once.
    max_streams: int = 3
    # How much disk the HLS segment cache may use, in MB.
    hls_cache_mb: int = 2048

    @property
    def db_path(self) -> Path:
        return self.data_dir / "reel.db"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / "thumbs"

    @property
    def hls_dir(self) -> Path:
        """HLS segments being served (a cache: emptied at startup)."""
        return self.data_dir / "hls"

    @property
    def images_dir(self) -> Path:
        """Uploaded images: images/tags, images/videos and images/folders."""
        return self.data_dir / "images"

    @property
    def tag_images_dir(self) -> Path:
        return self.images_dir / "tags"

    def check_data_dir(self) -> None:
        """Fail early, and clearly, if the data folder can't be written.

        A common Docker slip: a bind-mounted ./data that Docker created owned
        by root, while the app runs as an ordinary user.
        """
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=self.data_dir):
                pass
        except OSError as exc:
            raise SystemExit(
                f"Reel can't write to its data folder {self.data_dir} ({exc.strerror}). "
                f"It runs as uid {os.getuid()}: make the folder writable by that user, "
                f"e.g. `sudo chown -R {os.getuid()}:{os.getgid()} <the data folder>`."
            )

    def lock_data_dir(self) -> "DataLock":
        return DataLock(self.data_dir)

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            media_root=Path(os.environ.get("REEL_MEDIA_ROOT", "/media")).resolve(),
            data_dir=Path(os.environ.get("REEL_DATA_DIR", "./data")).resolve(),
            probe_workers=int(os.environ.get("REEL_PROBE_WORKERS", "4")),
            missing_grace=timedelta(days=float(os.environ.get("REEL_MISSING_GRACE_DAYS", "7"))),
            image_urls=_choice("REEL_IMAGE_URLS", ("internet", "lan", "off"), "internet"),
            max_streams=max(1, int(os.environ.get("REEL_MAX_STREAMS", "3"))),
            hls_cache_mb=max(100, int(os.environ.get("REEL_HLS_CACHE_MB", "2048"))),
        )


class DataFolderInUse(RuntimeError):
    """Another Reel already runs on this data folder."""


class DataLock:
    """Only one Reel may use a data folder: its scans, stream limits and HLS
    sessions live in the process, so a second one would fight the first. Held
    (an OS file lock on reel.lock) until release() or the process ends."""

    def __init__(self, data_dir: Path):
        import fcntl
        self._fd = os.open(data_dir / "reel.lock", os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self._fd)
            raise DataFolderInUse(
                f"Another Reel is already using the data folder {data_dir}. "
                "Only one can run per data folder: stop the other one first."
            )
        os.ftruncate(self._fd, 0)
        os.write(self._fd, f"{os.getpid()}\n".encode())

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)  # closing drops the lock
            self._fd = None
