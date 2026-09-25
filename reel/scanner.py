"""Walk a library folder and record every video in the database."""
import os
import re
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from .db import new_uid
from .paths import is_inside
from .probe import ProbeError, ProbeResult, classify, probe as ffprobe

VIDEO_EXTENSIONS = {
    "mp4", "m4v", "mov", "mkv", "webm", "avi", "wmv", "asf", "mpg", "mpeg",
    "m2ts", "mts", "ts", "vob", "flv", "3gp", "ogv", "divx",
}
IMAGE_EXTENSIONS = ("jpg", "jpeg", "png", "webp")
# Filenames that say nothing about the video, so the folder name is used instead.
GENERIC_STEMS = {"movie", "video", "film", "main", "feature"}
YEAR_PREFIX = re.compile(r"^((?:19|20)\d{2})[.\s_-]+(.+)$")
# How long a video that's no longer found stays in the catalog (hidden, with its
# tags and images) before it's removed. Covers a NAS that was briefly unmounted.
MISSING_GRACE = timedelta(days=7)

ProgressFn = Callable[[int, int], None]
ProbeFn = Callable[[Path], ProbeResult]


class ScanError(Exception):
    pass


@dataclass
class FoundVideo:
    rel_path: str
    title: str
    year: int | None
    poster_path: str | None
    size: int
    mtime: float


def is_video(filename: str) -> bool:
    if filename.startswith(".") or "." not in filename:
        return False
    return filename.rsplit(".", 1)[1].lower() in VIDEO_EXTENSIONS


def derive_title(rel_path: str, alone_in_folder: bool = False) -> tuple[str, int | None]:
    """'Tapes/1992.zoo-trip.mpg' -> ('zoo-trip', 1992); 'Classics/0360/movie.mp4' -> ('0360', None).

    A video that is the only one in its folder, or has a generic name like
    'movie', is named after its folder ('Drama/0902/rough-cut.mp4' -> '0902').
    """
    path = Path(rel_path)
    stem = path.stem
    if path.parent.name and (alone_in_folder or stem.lower() in GENERIC_STEMS):
        stem = path.parent.name
    match = YEAR_PREFIX.match(stem)
    if match:
        return match.group(2), int(match.group(1))
    return stem, None


def _find_image(names_lower: dict[str, str], stem: str) -> str | None:
    for ext in IMAGE_EXTENSIONS:
        name = names_lower.get(f"{stem.lower()}.{ext}")
        if name:
            return name
    return None


def find_poster(video_name: str, names_lower: dict[str, str]) -> str | None:
    """A video's preview: the image next to it with the same name.

    'zombie.mp4' -> 'zombie.png', 'movie.mp4' -> 'movie.jpg'. `names_lower` maps
    lowercased names in the video's folder to their real names. Folder images
    (folder.png) are folder previews only, never a video's.
    """
    return _find_image(names_lower, Path(video_name).stem)


@dataclass
class Walk:
    """What a walk over a library found, and what it couldn't read."""
    videos: list[FoundVideo]
    folder_art: dict[str, str]
    unreadable_folders: list[str]   # folders that couldn't be listed (relative)
    unreadable_files: set[str]      # videos listed but not readable (relative)
    outside_library: int = 0        # symlinks leading out of the library, skipped

    def protects(self, rel_path: str) -> bool:
        """Was this path hidden from the walk by something it couldn't read?

        Such paths may well still exist, so the scan must leave them alone.
        """
        if rel_path in self.unreadable_files:
            return True
        return any(rel_path == d or rel_path.startswith(d + "/") for d in self.unreadable_folders)


def walk_library(root: Path) -> Walk:
    """Find all videos under root, noting anything that couldn't be read."""
    videos: list[FoundVideo] = []
    folder_art: dict[str, str] = {}
    unreadable_folders: list[str] = []
    unreadable_files: set[str] = set()
    outside = 0

    def on_error(err: OSError) -> None:
        if Path(err.filename) == root:
            raise ScanError(f"Can't read library folder: {err.strerror}")
        # Keep going, but remember: nothing below this folder may be treated as gone.
        unreadable_folders.append(Path(err.filename).relative_to(root).as_posix())

    for dirpath, dirnames, filenames in os.walk(root, onerror=on_error):
        # Skip hidden folders (.zfs snapshots, .Trash, etc.).
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        rel_dir = Path(dirpath).relative_to(root)
        # Symlinks leading out of the library are ignored, videos and pictures alike.
        escaping = {f for f in filenames if os.path.islink(os.path.join(dirpath, f)) and not is_inside(root, rel_dir / f)}
        outside += sum(1 for f in escaping if is_video(f))
        filenames = [f for f in filenames if f not in escaping]
        names_lower = {f.lower(): f for f in filenames}
        video_names = sorted(f for f in filenames if is_video(f))

        art = _find_image(names_lower, "folder")
        if art:
            folder_art[rel_dir.as_posix() if rel_dir.parts else ""] = (rel_dir / art).as_posix()

        for name in video_names:
            rel_path = (rel_dir / name).as_posix()
            try:
                st = os.stat(os.path.join(dirpath, name))
            except FileNotFoundError:
                continue  # deleted while we were scanning: really gone
            except OSError:
                unreadable_files.add(rel_path)  # there, but unreadable right now
                continue
            # A lone video in a leaf folder (Drama/0902/rough-cut.mp4) is named after the folder;
            # a lone video beside other folders (Personal/loose.mp4) keeps its own name.
            alone = len(video_names) == 1 and not dirnames
            title, year = derive_title(rel_path, alone_in_folder=alone)
            poster = find_poster(name, names_lower)
            videos.append(FoundVideo(
                rel_path=rel_path,
                title=title,
                year=year,
                poster_path=(rel_dir / poster).as_posix() if poster else None,
                size=st.st_size,
                mtime=st.st_mtime,
            ))
    return Walk(videos, folder_art, sorted(unreadable_folders), unreadable_files, outside)


def _safe_probe(probe_fn: ProbeFn, path: Path) -> ProbeResult:
    try:
        return probe_fn(path)
    except (ProbeError, OSError, ValueError) as exc:
        return ProbeResult(error=str(exc) or type(exc).__name__)


def scan_library(
    conn: sqlite3.Connection,
    library_id: int,
    *,
    probe_fn: ProbeFn = ffprobe,
    workers: int = 4,
    on_progress: ProgressFn | None = None,
    missing_grace: timedelta = MISSING_GRACE,
) -> dict:
    """Bring the catalog for one library in line with what's on disk.

    Only new or changed files (by size and modified time) are probed again, as are
    files whose last probe failed. Returns counts of what changed.

    Nothing is thrown away because of something the scan couldn't read: videos in
    unreadable folders, or that couldn't be read themselves, are left as they are.
    A video that's really gone is first marked missing (hidden, but its tags and
    images kept), and only removed once it has been missing for `missing_grace`.
    """
    lib = conn.execute("SELECT path FROM libraries WHERE id = ?", (library_id,)).fetchone()
    if lib is None:
        raise ScanError("Library not found.")
    root = Path(lib["path"])
    # If the NAS isn't mounted, stop here rather than "deleting" every video.
    if not root.is_dir():
        raise ScanError(f"Library folder is missing: {root}")

    walk = walk_library(root)
    videos = walk.videos
    existing = {
        row["rel_path"]: row
        for row in conn.execute(
            "SELECT id, rel_path, size, mtime, probe_error, missing_since FROM media_items WHERE library_id = ?",
            (library_id,),
        )
    }
    # A library that used to have videos but now reads as completely empty is
    # almost always an unmounted NAS (an empty mount point), not a real deletion.
    present = sum(1 for row in existing.values() if not row["missing_since"])
    if present and not videos and not walk.unreadable_folders and not walk.unreadable_files:
        raise ScanError(
            f"The library folder {root} is empty, but {present} videos were in it. "
            "Is the NAS mounted? Nothing was changed. "
            "(If you really deleted everything, remove the library instead.)"
        )

    unchanged, to_probe = [], []
    for video in videos:
        row = existing.get(video.rel_path)
        if row and row["size"] == video.size and row["mtime"] == video.mtime and not row["probe_error"]:
            unchanged.append(video)
        else:
            to_probe.append(video)

    total = len(videos)
    done = len(unchanged)
    if on_progress:
        on_progress(done, total)

    # Titles and posters are cheap to recompute, so refresh them for unchanged files
    # too; that picks up a poster added next to an existing video.
    conn.executemany(
        """
        UPDATE media_items SET title = ?, year = ?, poster_path = ?, missing_since = NULL
        WHERE library_id = ? AND rel_path = ?
        """,
        [(v.title, v.year, v.poster_path, library_id, v.rel_path) for v in unchanged],
    )
    conn.commit()

    added = updated = failed = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = pool.map(lambda v: (v, _safe_probe(probe_fn, root / v.rel_path)), to_probe)
        for video, result in results:
            mode = classify(result, Path(video.rel_path).suffix)
            conn.execute(
                """
                INSERT INTO media_items (
                    uid, library_id, rel_path, title, year, poster_path, size, mtime,
                    container, video_codec, audio_codec, pix_fmt, width, height,
                    duration, interlaced, play_mode, probe_error, scanned_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT (library_id, rel_path) DO UPDATE SET
                    title = excluded.title, year = excluded.year,
                    poster_path = excluded.poster_path, size = excluded.size,
                    mtime = excluded.mtime, container = excluded.container,
                    video_codec = excluded.video_codec, audio_codec = excluded.audio_codec,
                    pix_fmt = excluded.pix_fmt, width = excluded.width,
                    height = excluded.height, duration = excluded.duration,
                    interlaced = excluded.interlaced, play_mode = excluded.play_mode,
                    probe_error = excluded.probe_error, scanned_at = excluded.scanned_at,
                    missing_since = NULL
                """,
                (
                    new_uid(), library_id, video.rel_path, video.title, video.year, video.poster_path,
                    video.size, video.mtime, result.container, result.video_codec,
                    result.audio_codec, result.pix_fmt, result.width, result.height,
                    result.duration, int(result.interlaced), mode, result.error,
                ),
            )
            # Commit per file so a long scan never holds the database write lock.
            conn.commit()
            if video.rel_path in existing:
                updated += 1
            else:
                added += 1
            if result.error:
                failed += 1
            done += 1
            if on_progress:
                on_progress(done, total)

    # Videos the walk didn't see: missing first, removed after the grace period.
    # Anything hidden by an unreadable folder or file is left exactly as it is.
    seen = {v.rel_path for v in videos}
    newly_missing, to_remove = [], []
    grace = f"-{int(missing_grace.total_seconds())} seconds"
    for path, row in existing.items():
        if path in seen or walk.protects(path):
            continue
        if not row["missing_since"]:
            newly_missing.append(row["id"])
    conn.executemany(
        "UPDATE media_items SET missing_since = datetime('now') WHERE id = ?", [(i,) for i in newly_missing]
    )
    for path, row in existing.items():
        if path in seen or walk.protects(path):
            continue
        expired = conn.execute(
            "SELECT missing_since <= datetime('now', ?) FROM media_items WHERE id = ?", (grace, row["id"])
        ).fetchone()[0]
        if expired:
            to_remove.append(row["id"])
    conn.executemany("DELETE FROM media_items WHERE id = ?", [(i,) for i in to_remove])

    # Folder art is rebuilt from the walk, except under folders it couldn't read.
    for row in conn.execute("SELECT rel_dir FROM folder_art WHERE library_id = ?", (library_id,)).fetchall():
        if not walk.protects(row["rel_dir"]):
            conn.execute("DELETE FROM folder_art WHERE library_id = ? AND rel_dir = ?", (library_id, row["rel_dir"]))
    conn.executemany(
        "INSERT OR REPLACE INTO folder_art (library_id, rel_dir, art_path) VALUES (?, ?, ?)",
        [(library_id, d, art) for d, art in walk.folder_art.items()],
    )

    warning = None
    if walk.unreadable_folders or walk.unreadable_files:
        parts = []
        if walk.unreadable_folders:
            names = ", ".join(walk.unreadable_folders[:5]) + ("…" if len(walk.unreadable_folders) > 5 else "")
            parts.append(f"couldn't read {len(walk.unreadable_folders)} folder(s): {names}")
        if walk.unreadable_files:
            parts.append(f"couldn't read {len(walk.unreadable_files)} video file(s)")
        warning = "The scan " + " and ".join(parts) + ". Their videos were kept as they were."
    conn.execute(
        """
        UPDATE libraries SET last_scan_at = datetime('now'), last_scan_error = NULL, last_scan_warning = ?
        WHERE id = ?
        """,
        (warning, library_id),
    )
    conn.commit()
    return {
        "total": total,
        "added": added,
        "updated": updated,
        "unchanged": len(unchanged),
        "failed": failed,
        "missing": len(newly_missing) - len([i for i in to_remove if i in newly_missing]),
        "removed": len(to_remove),
        "unreadable_folders": walk.unreadable_folders,
        "unreadable_files": len(walk.unreadable_files),
        "outside_library": walk.outside_library,
    }
