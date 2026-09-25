"""Pictures you set yourself, for tags, videos and folders: one way to store them.

A picture you upload (or snap from a video in the player) wins over one on the
NAS (folder.png, or zombie.png beside zombie.mp4); removing it shows the NAS
one again. Pictures are stored in the data folder, never on the NAS, one file
per picture, never overwritten:

    images/tags/<tag uuid>-<version>.jpg
    images/videos/<video uuid>-<version>.jpg
    images/folders/<folder picture uuid>-<version>.jpg   (one folder_images row per folder)

Setting one always goes: write the new file, record its version (in one
commit), then delete the previous file. Removing: forget it, then delete the
file. prune() only deletes unreferenced files older than ORPHAN_GRACE_SECONDS,
so a file that's written but not yet recorded is never removed from under an
upload, and a failure in between leaves at worst an unused file for prune().

The owners differ only in where the version lives: a column of the tag's or
video's own row, or a folder_images row (a folder isn't a row of its own, so
that row has its own uuid, which names the file).
"""
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .catalog import NotFound, clean_dir, descendants
from .db import new_uid
from .images import ORPHAN_GRACE_SECONDS, save_upload, upload_name


def picture_path(images_dir: Path, kind: str, file_id: str, version: str) -> Path:
    return images_dir / kind / upload_name(file_id, version)


def _new_version() -> str:
    # Changes with every picture, so the browser never shows a stale one.
    return new_uid()[:8]


# ---- Owners -------------------------------------------------------------------------------


@dataclass
class TagPicture:
    uid: str
    kind: str = "tags"

    def check(self, conn: sqlite3.Connection) -> None:
        self.current(conn)

    def current(self, conn: sqlite3.Connection) -> tuple[str, str | None]:
        """(the file's id, the current version or None)."""
        row = conn.execute("SELECT image_version FROM tags WHERE uid = ?", (self.uid,)).fetchone()
        if row is None:
            raise NotFound("Tag not found.")
        return self.uid, row["image_version"]

    def record(self, conn: sqlite3.Connection, file_id: str, version: str | None) -> None:
        conn.execute("UPDATE tags SET image_version = ? WHERE uid = ?", (version, self.uid))


@dataclass
class VideoPicture:
    uid: str
    kind: str = "videos"

    def check(self, conn: sqlite3.Connection) -> None:
        self.current(conn)

    def current(self, conn: sqlite3.Connection) -> tuple[str, str | None]:
        row = conn.execute("SELECT custom_image FROM media_items WHERE uid = ?", (self.uid,)).fetchone()
        if row is None:
            raise NotFound("Video not found.")
        return self.uid, row["custom_image"]

    def record(self, conn: sqlite3.Connection, file_id: str, version: str | None) -> None:
        conn.execute("UPDATE media_items SET custom_image = ? WHERE uid = ?", (version, self.uid))


@dataclass
class FolderPicture:
    library_id: int
    rel_dir: str
    kind: str = "folders"
    _fresh_id: str = field(default_factory=new_uid)

    def __post_init__(self):
        self.rel_dir = clean_dir(self.rel_dir)

    def check(self, conn: sqlite3.Connection) -> None:
        """The folder must hold videos (a folder only exists in Reel if it does)."""
        if self.rel_dir and not folder_has_videos(conn, self.library_id, self.rel_dir):
            raise NotFound("Folder not found.")

    def current(self, conn: sqlite3.Connection) -> tuple[str, str | None]:
        row = conn.execute(
            "SELECT uid, version FROM folder_images WHERE library_id = ? AND rel_dir = ?",
            (self.library_id, self.rel_dir),
        ).fetchone()
        return (row["uid"], row["version"]) if row else (self._fresh_id, None)

    def record(self, conn: sqlite3.Connection, file_id: str, version: str | None) -> None:
        if version is None:
            conn.execute("DELETE FROM folder_images WHERE library_id = ? AND rel_dir = ?",
                         (self.library_id, self.rel_dir))
            return
        # The uid and version are written together, so the row always names the
        # file just saved, even if another first upload to this folder got in
        # between (the loser's file is then an orphan, removed by prune()).
        conn.execute(
            """
            INSERT INTO folder_images (uid, library_id, rel_dir, version) VALUES (?, ?, ?, ?)
            ON CONFLICT (library_id, rel_dir) DO UPDATE SET uid = excluded.uid, version = excluded.version
            """,
            (file_id, self.library_id, self.rel_dir, version),
        )


Owner = TagPicture | VideoPicture | FolderPicture


def folder_has_videos(conn: sqlite3.Connection, library_id: int, rel_dir: str) -> bool:
    low, high = descendants(rel_dir)
    return conn.execute(
        """
        SELECT 1 FROM media_items WHERE library_id = ?
          AND (parent_dir = ? OR (parent_dir >= ? AND parent_dir < ?)) LIMIT 1
        """,
        (library_id, rel_dir, low, high),
    ).fetchone() is not None


# ---- Setting and removing ----------------------------------------------------------------


def set_picture(conn: sqlite3.Connection, images_dir: Path, owner: Owner, write: Callable[[Path], None]) -> str:
    """Give `owner` a new picture: `write(path)` makes the file. Returns its version."""
    owner.check(conn)
    file_id, old = owner.current(conn)
    version = _new_version()
    write(picture_path(images_dir, owner.kind, file_id, version))   # 1. the new file
    owner.record(conn, file_id, version)
    conn.commit()                                                    # 2. point at it
    if old:                                                          # 3. drop the old one
        picture_path(images_dir, owner.kind, file_id, old).unlink(missing_ok=True)
    return version


def set_uploaded(conn: sqlite3.Connection, images_dir: Path, owner: Owner, data: bytes) -> str:
    """An uploaded (or downloaded) image; raises ThumbnailError if it isn't one."""
    return set_picture(conn, images_dir, owner, lambda out: save_upload(data, out))


def remove_picture(conn: sqlite3.Connection, images_dir: Path, owner: Owner) -> None:
    """Forget the owner's picture, then delete its file (a NAS picture, if any, shows again)."""
    file_id, old = owner.current(conn)
    if old is None:
        return
    owner.record(conn, file_id, None)
    conn.commit()
    picture_path(images_dir, owner.kind, file_id, old).unlink(missing_ok=True)


def picture_file(conn: sqlite3.Connection, images_dir: Path, owner: Owner) -> Path | None:
    """The owner's picture file, if it has one that exists."""
    file_id, version = owner.current(conn)
    if version is None:
        return None
    path = picture_path(images_dir, owner.kind, file_id, version)
    return path if path.is_file() else None


# ---- Housekeeping ---------------------------------------------------------------------------


def _referenced(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """The file names the database points at, by kind."""
    return {
        "videos": {upload_name(u, v) for u, v in conn.execute(
            "SELECT uid, custom_image FROM media_items WHERE custom_image IS NOT NULL")},
        "folders": {upload_name(u, v) for u, v in conn.execute("SELECT uid, version FROM folder_images")},
        "tags": {upload_name(u, v) for u, v in conn.execute(
            "SELECT uid, image_version FROM tags WHERE image_version IS NOT NULL")},
    }


def prune(conn: sqlite3.Connection, images_dir: Path, *, grace_seconds: float = ORPHAN_GRACE_SECONDS) -> int:
    """Delete pictures nothing points at any more. Returns how many.

    Only files older than the grace period go: a file that was just written may
    belong to an upload that hasn't recorded it in the database yet.
    """
    # Folder pictures whose folder no longer holds any videos (or whose library is gone).
    for row in conn.execute("SELECT uid, library_id, rel_dir FROM folder_images").fetchall():
        if row["rel_dir"] and not folder_has_videos(conn, row["library_id"], row["rel_dir"]):
            conn.execute("DELETE FROM folder_images WHERE uid = ?", (row["uid"],))
    conn.commit()
    keep = _referenced(conn)
    cutoff = time.time() - grace_seconds
    removed = 0
    for kind, names in keep.items():
        folder = images_dir / kind
        if not folder.is_dir():
            continue
        for path in folder.glob("*.jpg"):
            if path.name in names:
                continue
            try:
                if path.stat().st_mtime > cutoff:
                    continue  # too new: may be an upload in progress
            except FileNotFoundError:
                continue
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def adopt_unversioned_files(conn: sqlite3.Connection, images_dir: Path) -> None:
    """Pictures used to be stored as "<uuid>.jpg"; rename them to "<uuid>-<version>.jpg"."""
    rows = {
        "videos": conn.execute("SELECT uid, custom_image FROM media_items WHERE custom_image IS NOT NULL").fetchall(),
        "folders": conn.execute("SELECT uid, version FROM folder_images").fetchall(),
        "tags": conn.execute("SELECT uid, image_version FROM tags WHERE image_version IS NOT NULL").fetchall(),
    }
    for kind, pairs in rows.items():
        for uid, version in pairs:
            old = images_dir / kind / f"{uid}.jpg"
            new = picture_path(images_dir, kind, uid, version)
            if old.is_file() and not new.exists():
                old.replace(new)


def move_old_tag_images(data_dir: Path, images_dir: Path) -> None:
    """Tag pictures used to live in <data>/tag-images; they're now in images/tags."""
    old = data_dir / "tag-images"
    if not old.is_dir():
        return
    new = images_dir / "tags"
    new.mkdir(parents=True, exist_ok=True)
    for path in old.glob("*.jpg"):
        path.replace(new / path.name)
    try:
        old.rmdir()
    except OSError:
        pass  # something else is in there; leave it be
