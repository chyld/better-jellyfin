import os
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path


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

    @property
    def db_path(self) -> Path:
        return self.data_dir / "reel.db"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / "thumbs"

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

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            media_root=Path(os.environ.get("REEL_MEDIA_ROOT", "/media")).resolve(),
            data_dir=Path(os.environ.get("REEL_DATA_DIR", "./data")).resolve(),
            probe_workers=int(os.environ.get("REEL_PROBE_WORKERS", "4")),
            missing_grace=timedelta(days=float(os.environ.get("REEL_MISSING_GRACE_DAYS", "7"))),
        )
