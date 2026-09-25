"""Walk a library folder and record every video in the database."""
import hashlib
import os
import re
import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from .db import new_uid
from .paths import is_inside
from .sorting import parent_dir, sort_key
from .probe import PROBE_VERSION, ProbeError, ProbeResult, probe as ffprobe

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
FINGERPRINT_CHUNK = 64 * 1024

ProgressFn = Callable[[int, int], None]
ProbeFn = Callable[[Path], ProbeResult]


class ScanError(Exception):
    pass


class ScanCancelled(Exception):
    """The scan was asked to stop (Reel is shutting down). Work already committed
    is kept; nothing is marked missing, since the walk may be incomplete."""


def _check(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise ScanCancelled("The scan was stopped.")


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
    folders: set[str] = None        # every folder that was listed ('' is the root)

    def protects(self, rel_path: str) -> bool:
        """Was this path hidden from the walk by something it couldn't read?

        Such paths may well still exist, so the scan must leave them alone.
        """
        if rel_path in self.unreadable_files:
            return True
        return any(rel_path == d or rel_path.startswith(d + "/") for d in self.unreadable_folders)


def walk_library(root: Path, cancel: threading.Event | None = None) -> Walk:
    """Find all videos under root, noting anything that couldn't be read."""
    videos: list[FoundVideo] = []
    folder_art: dict[str, str] = {}
    unreadable_folders: list[str] = []
    unreadable_files: set[str] = set()
    outside = 0
    folders: set[str] = set()

    def on_error(err: OSError) -> None:
        if Path(err.filename) == root:
            raise ScanError(f"Can't read library folder: {err.strerror}")
        # Keep going, but remember: nothing below this folder may be treated as gone.
        unreadable_folders.append(Path(err.filename).relative_to(root).as_posix())

    for dirpath, dirnames, filenames in os.walk(root, onerror=on_error):
        _check(cancel)
        # Skip hidden folders (.zfs snapshots, .Trash, etc.).
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        rel_dir = Path(dirpath).relative_to(root)
        folders.add(rel_dir.as_posix() if rel_dir.parts else "")
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
    return Walk(videos, folder_art, sorted(unreadable_folders), unreadable_files, outside, folders)


def fingerprint(path: Path, size: int) -> str | None:
    """A cheap identity for a file's contents: its size plus hashes of its first
    and last 64 KB. Survives renames and moves; changes if the file does."""
    digest = hashlib.sha256(str(size).encode())
    try:
        with open(path, "rb") as f:
            digest.update(f.read(FINGERPRINT_CHUNK))
            if size > 2 * FINGERPRINT_CHUNK:
                f.seek(-FINGERPRINT_CHUNK, os.SEEK_END)
                digest.update(f.read(FINGERPRINT_CHUNK))
    except OSError:
        return None
    return digest.hexdigest()[:32]


def _match_moves(new_videos: list[FoundVideo], new_prints: dict[str, str | None], gone: list[dict]) -> list[tuple[dict, FoundVideo]]:
    """Pair new files with vanished catalog rows that are clearly the same file.

    Conservative: only a unique match on both sides counts (same fingerprint,
    same extension, not empty). Anything ambiguous is left as new + missing.
    """
    def key(fp: str | None, rel_path: str, size: int):
        return (fp, Path(rel_path).suffix.lower()) if fp and size > 0 else None

    new_by_key: dict = {}
    for video in new_videos:
        k = key(new_prints.get(video.rel_path), video.rel_path, video.size)
        if k:
            new_by_key.setdefault(k, []).append(video)
    gone_by_key: dict = {}
    for row in gone:
        k = key(row["fingerprint"], row["rel_path"], row["size"])
        if k:
            gone_by_key.setdefault(k, []).append(row)
    return [
        (rows[0], new_by_key[k][0])
        for k, rows in gone_by_key.items()
        if len(rows) == 1 and len(new_by_key.get(k, [])) == 1
    ]


def _renamed_folders(moves: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """From file moves, the folder renames they imply.

    Tapes/1990s/a.mpg -> Home Tapes/1990s/a.mpg implies Tapes -> Home Tapes: the
    paths share their tail (1990s/a.mpg), and what's left is the rename.
    """
    renames = []
    for old, new in moves:
        o, n = old.split("/"), new.split("/")
        k = 0
        while k < min(len(o), len(n)) and o[-1 - k] == n[-1 - k]:
            k += 1
        if k and o[:-k] != n[:-k]:
            renames.append(("/".join(o[:-k]), "/".join(n[:-k])))
    return renames


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
    cancel: threading.Event | None = None,
) -> dict:
    """Bring the catalog for one library in line with what's on disk.

    Only new or changed files (by size and modified time) are probed again, as are
    files whose last probe failed. Returns counts of what changed.

    Nothing is thrown away because of something the scan couldn't read: videos in
    unreadable folders, or that couldn't be read themselves, are left as they are.
    A video that's really gone is first marked missing (hidden, but its tags and
    images kept), and only removed once it has been missing for `missing_grace`.

    Setting `cancel` stops the scan soon (between folders, or between files),
    raising ScanCancelled; videos already recorded stay recorded.
    """
    lib = conn.execute("SELECT path FROM libraries WHERE id = ?", (library_id,)).fetchone()
    if lib is None:
        raise ScanError("Library not found.")
    root = Path(lib["path"])
    # If the NAS isn't mounted, stop here rather than "deleting" every video.
    if not root.is_dir():
        raise ScanError(f"Library folder is missing: {root}")

    walk = walk_library(root, cancel)
    videos = walk.videos
    existing = {
        row["rel_path"]: dict(row)
        for row in conn.execute(
            """
            SELECT id, rel_path, size, mtime, probe_error, missing_since, fingerprint, probe_version
            FROM media_items WHERE library_id = ?
            """,
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

    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        seen = {v.rel_path for v in videos}

        # Moved or renamed files: a new path whose contents match a vanished row
        # takes over that row, keeping its id, tags, pictures (and later, progress).
        new_videos = [v for v in videos if v.rel_path not in existing]
        gone = [row for path, row in existing.items() if path not in seen and not walk.protects(path)]
        prints: dict[str, str | None] = {}
        if new_videos and any(row["fingerprint"] for row in gone):
            prints = dict(zip(
                (v.rel_path for v in new_videos),
                pool.map(lambda v: None if cancel is not None and cancel.is_set()
                         else fingerprint(root / v.rel_path, v.size), new_videos),
            ))
            _check(cancel)  # before matching moves on a partial set of fingerprints
        moves = _match_moves(new_videos, prints, gone)
        for row, video in moves:
            conn.execute(
                """
                UPDATE media_items SET rel_path = ?, title = ?, year = ?, poster_path = ?,
                    size = ?, mtime = ?, missing_since = NULL, parent_dir = ?, title_key = ?
                WHERE id = ?
                """,
                (video.rel_path, video.title, video.year, video.poster_path, video.size, video.mtime,
                 parent_dir(video.rel_path), sort_key(video.title, video.rel_path), row["id"]),
            )
            del existing[row["rel_path"]]
            existing[video.rel_path] = {**row, "rel_path": video.rel_path, "size": video.size,
                                        "mtime": video.mtime, "missing_since": None}
        folder_renames = _renamed_folders([(row["rel_path"], video.rel_path) for row, video in moves])
        conn.commit()

        unchanged, to_probe = [], []
        for video in videos:
            row = existing.get(video.rel_path)
            if (
                row and row["size"] == video.size and row["mtime"] == video.mtime
                and not row["probe_error"] and row["probe_version"] >= PROBE_VERSION
            ):
                unchanged.append(video)
            else:
                to_probe.append(video)

        total = len(videos)
        done = len(unchanged)
        if on_progress:
            on_progress(done, total)

        # Rows from before fingerprints get one (a quick read, not a re-probe), so a
        # later move can be recognised.
        backfill = [v for v in unchanged if not existing[v.rel_path]["fingerprint"]]
        backfill_prints = list(pool.map(
            lambda v: None if cancel is not None and cancel.is_set() else fingerprint(root / v.rel_path, v.size),
            backfill))
        _check(cancel)
        conn.executemany(
            "UPDATE media_items SET fingerprint = ? WHERE library_id = ? AND rel_path = ?",
            [(fp, library_id, v.rel_path) for v, fp in zip(backfill, backfill_prints)],
        )

        # Titles and posters are cheap to recompute, so refresh them for unchanged files
        # too; that picks up a poster added next to an existing video.
        conn.executemany(
            """
            UPDATE media_items SET title = ?, year = ?, poster_path = ?, missing_since = NULL, title_key = ?
            WHERE library_id = ? AND rel_path = ?
            """,
            [(v.title, v.year, v.poster_path, sort_key(v.title, v.rel_path), library_id, v.rel_path) for v in unchanged],
        )
        conn.commit()

        def stopped() -> bool:
            return cancel is not None and cancel.is_set()

        def examine(video: FoundVideo):
            if stopped():
                return video, None, None  # skipped; the loop below stops
            fp = prints.get(video.rel_path) or fingerprint(root / video.rel_path, video.size)
            if stopped():                 # asked to stop while fingerprinting: don't start ffprobe
                return video, None, None
            return video, _safe_probe(probe_fn, root / video.rel_path), fp

        added = updated = failed = 0
        for video, result, fp in pool.map(examine, to_probe):
            _check(cancel)
            conn.execute(
                """
                INSERT INTO media_items (
                    uid, library_id, rel_path, title, year, poster_path, size, mtime,
                    container, video_codec, audio_codec, pix_fmt, width, height,
                    duration, interlaced, probe_error, fingerprint, probe_version, parent_dir, title_key,
                    scanned_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT (library_id, rel_path) DO UPDATE SET
                    title = excluded.title, year = excluded.year,
                    poster_path = excluded.poster_path, size = excluded.size,
                    mtime = excluded.mtime, container = excluded.container,
                    video_codec = excluded.video_codec, audio_codec = excluded.audio_codec,
                    pix_fmt = excluded.pix_fmt, width = excluded.width,
                    height = excluded.height, duration = excluded.duration,
                    interlaced = excluded.interlaced,
                    probe_error = excluded.probe_error, scanned_at = excluded.scanned_at,
                    fingerprint = excluded.fingerprint, probe_version = excluded.probe_version,
                    parent_dir = excluded.parent_dir, title_key = excluded.title_key,
                    missing_since = NULL
                """,
                (
                    new_uid(), library_id, video.rel_path, video.title, video.year, video.poster_path,
                    video.size, video.mtime, result.container, result.video_codec,
                    result.audio_codec, result.pix_fmt, result.width, result.height,
                    result.duration, int(result.interlaced), result.error,
                    fp, PROBE_VERSION, parent_dir(video.rel_path), sort_key(video.title, video.rel_path),
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
    finally:
        # On a stop, drop the probes not started yet (the running ones finish).
        pool.shutdown(wait=True, cancel_futures=True)
    _check(cancel)

    # Videos the walk didn't see: missing first, removed after the grace period.
    # Anything hidden by an unreadable folder or file is left exactly as it is.
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

    # Uploaded folder pictures follow a renamed folder, when the moved files say
    # clearly where it went and nothing is already there.
    for row in conn.execute("SELECT uid, rel_dir FROM folder_images WHERE library_id = ?", (library_id,)).fetchall():
        old = row["rel_dir"]
        if not old or old in walk.folders or walk.protects(old):
            continue
        targets = {new + old[len(src):] for src, new in folder_renames if old == src or old.startswith(src + "/")}
        if len(targets) == 1:
            (target,) = targets
            taken = conn.execute(
                "SELECT 1 FROM folder_images WHERE library_id = ? AND rel_dir = ?", (library_id, target)
            ).fetchone()
            if target in walk.folders and not taken:
                conn.execute("UPDATE folder_images SET rel_dir = ? WHERE uid = ?", (target, row["uid"]))

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
        "moved": len(moves),
        "unchanged": len(unchanged),
        "failed": failed,
        "missing": len(newly_missing) - len([i for i in to_remove if i in newly_missing]),
        "removed": len(to_remove),
        "unreadable_folders": walk.unreadable_folders,
        "unreadable_files": len(walk.unreadable_files),
        "outside_library": walk.outside_library,
    }
