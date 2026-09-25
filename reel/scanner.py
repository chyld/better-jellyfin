"""Walk a library folder and record every video in the database."""
import os
import re
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .db import new_uid
from .probe import ProbeError, ProbeResult, classify, probe as ffprobe

VIDEO_EXTENSIONS = {
    "mp4", "m4v", "mov", "mkv", "webm", "avi", "wmv", "asf", "mpg", "mpeg",
    "m2ts", "mts", "ts", "vob", "flv", "3gp", "ogv", "divx",
}
IMAGE_EXTENSIONS = ("jpg", "jpeg", "png", "webp")
# Filenames that say nothing about the video, so the folder name is used instead.
GENERIC_STEMS = {"movie", "video", "film", "main", "feature"}
YEAR_PREFIX = re.compile(r"^((?:19|20)\d{2})[.\s_-]+(.+)$")

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


def walk_library(root: Path) -> tuple[list[FoundVideo], dict[str, str]]:
    """Find all videos under root. Returns (videos, folder art by relative folder)."""
    videos: list[FoundVideo] = []
    folder_art: dict[str, str] = {}

    def on_error(err: OSError) -> None:
        # A folder we can't read shouldn't abort the whole scan, except the root.
        if Path(err.filename) == root:
            raise ScanError(f"Can't read library folder: {err.strerror}")

    for dirpath, dirnames, filenames in os.walk(root, onerror=on_error):
        # Skip hidden folders (.zfs snapshots, .Trash, etc.).
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        rel_dir = Path(dirpath).relative_to(root)
        names_lower = {f.lower(): f for f in filenames}
        video_names = sorted(f for f in filenames if is_video(f))

        art = _find_image(names_lower, "folder")
        if art:
            folder_art[rel_dir.as_posix() if rel_dir.parts else ""] = (rel_dir / art).as_posix()

        for name in video_names:
            try:
                st = os.stat(os.path.join(dirpath, name))
            except OSError:
                continue  # vanished or unreadable mid-scan
            rel_path = (rel_dir / name).as_posix()
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
    return videos, folder_art


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
) -> dict:
    """Bring the catalog for one library in line with what's on disk.

    Only new or changed files (by size and modified time) are probed again, as are
    files whose last probe failed. Returns counts of what changed.
    """
    lib = conn.execute("SELECT path FROM libraries WHERE id = ?", (library_id,)).fetchone()
    if lib is None:
        raise ScanError("Library not found.")
    root = Path(lib["path"])
    # If the NAS isn't mounted, stop here rather than "deleting" every video.
    if not root.is_dir():
        raise ScanError(f"Library folder is missing: {root}")

    videos, folder_art = walk_library(root)
    existing = {
        row["rel_path"]: row
        for row in conn.execute(
            "SELECT id, rel_path, size, mtime, probe_error FROM media_items WHERE library_id = ?",
            (library_id,),
        )
    }

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
        "UPDATE media_items SET title = ?, year = ?, poster_path = ? WHERE library_id = ? AND rel_path = ?",
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
                    probe_error = excluded.probe_error, scanned_at = excluded.scanned_at
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

    seen = {v.rel_path for v in videos}
    gone = [row["id"] for path, row in existing.items() if path not in seen]
    conn.executemany("DELETE FROM media_items WHERE id = ?", [(i,) for i in gone])

    conn.execute("DELETE FROM folder_art WHERE library_id = ?", (library_id,))
    conn.executemany(
        "INSERT INTO folder_art (library_id, rel_dir, art_path) VALUES (?, ?, ?)",
        [(library_id, d, art) for d, art in folder_art.items()],
    )
    conn.execute(
        "UPDATE libraries SET last_scan_at = datetime('now'), last_scan_error = NULL WHERE id = ?",
        (library_id,),
    )
    conn.commit()
    return {
        "total": total,
        "added": added,
        "updated": updated,
        "removed": len(gone),
        "unchanged": len(unchanged),
        "failed": failed,
    }
