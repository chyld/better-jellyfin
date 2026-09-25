"""Images you upload for folders and videos that have none on the NAS.

A NAS image (folder.png, or zombie.png beside zombie.mp4) always wins; an
uploaded image is only shown when there isn't one. Uploads are stored in the
data folder, never on the NAS, one file per upload (see images.py for why):

    images/videos/<video uuid>-<version>.jpg
    images/folders/<folder image uuid>-<version>.jpg   (one row per folder in folder_images)
    images/tags/<tag uuid>-<version>.jpg               (see tags.py)
"""
import sqlite3
import time
from pathlib import Path

from .browse import NotFound, clean_dir
from .db import new_uid
from .images import ORPHAN_GRACE_SECONDS, save_upload, upload_name


class ImageConflict(Exception):
    """The folder or video already has an image on the NAS."""


def _version() -> str:
    # Changes with every upload, so the browser never shows a stale picture.
    return new_uid()[:8]


# ---- Videos ---------------------------------------------------------------------------


def _video(conn: sqlite3.Connection, item_uid: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, uid, poster_path, custom_image FROM media_items WHERE uid = ?", (item_uid,)
    ).fetchone()
    if row is None:
        raise NotFound("Video not found.")
    return row


def video_image_path(images_dir: Path, item_uid: str, version: str) -> Path:
    return images_dir / "videos" / upload_name(item_uid, version)


def set_video_image(conn: sqlite3.Connection, images_dir: Path, item_uid: str, data: bytes) -> dict:
    row = _video(conn, item_uid)
    if row["poster_path"]:
        raise ImageConflict(f"This video already has an image on the NAS ({Path(row['poster_path']).name}).")
    version = _version()
    save_upload(data, video_image_path(images_dir, row["uid"], version))   # 1. the new file
    conn.execute("UPDATE media_items SET custom_image = ? WHERE id = ?", (version, row["id"]))
    conn.commit()                                                           # 2. point at it
    if row["custom_image"]:                                                 # 3. drop the old one
        video_image_path(images_dir, row["uid"], row["custom_image"]).unlink(missing_ok=True)
    return {"custom_image": version}


def remove_video_image(conn: sqlite3.Connection, images_dir: Path, item_uid: str) -> dict:
    row = _video(conn, item_uid)
    conn.execute("UPDATE media_items SET custom_image = NULL WHERE id = ?", (row["id"],))
    conn.commit()
    if row["custom_image"]:
        video_image_path(images_dir, row["uid"], row["custom_image"]).unlink(missing_ok=True)
    return {"custom_image": None}


# ---- Folders --------------------------------------------------------------------------


def _folder(conn: sqlite3.Connection, library_id: int, rel_dir: str) -> str:
    """Check the folder exists in the library's catalog; returns its clean path."""
    rel_dir = clean_dir(rel_dir)
    if rel_dir:
        prefix = rel_dir + "/"
        inside = conn.execute(
            "SELECT 1 FROM media_items WHERE library_id = ? AND substr(rel_path, 1, ?) = ? LIMIT 1",
            (library_id, len(prefix), prefix),
        ).fetchone()
        if not inside:
            raise NotFound("Folder not found.")
    return rel_dir


def folder_image_path(images_dir: Path, image_uid: str, version: str) -> Path:
    return images_dir / "folders" / upload_name(image_uid, version)


def custom_folder_image(conn: sqlite3.Connection, library_id: int, rel_dir: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT uid, version FROM folder_images WHERE library_id = ? AND rel_dir = ?", (library_id, rel_dir)
    ).fetchone()


def set_folder_image(conn: sqlite3.Connection, images_dir: Path, library_id: int, rel_dir: str, data: bytes) -> dict:
    rel_dir = _folder(conn, library_id, rel_dir)
    art = conn.execute(
        "SELECT art_path FROM folder_art WHERE library_id = ? AND rel_dir = ?", (library_id, rel_dir)
    ).fetchone()
    if art:
        raise ImageConflict(f"This folder already has an image on the NAS ({Path(art['art_path']).name}).")
    existing = custom_folder_image(conn, library_id, rel_dir)
    image_uid = existing["uid"] if existing else new_uid()
    version = _version()
    save_upload(data, folder_image_path(images_dir, image_uid, version))
    conn.execute(
        """
        INSERT INTO folder_images (uid, library_id, rel_dir, version) VALUES (?, ?, ?, ?)
        ON CONFLICT (library_id, rel_dir) DO UPDATE SET version = excluded.version
        """,
        (image_uid, library_id, rel_dir, version),
    )
    conn.commit()
    if existing:
        folder_image_path(images_dir, existing["uid"], existing["version"]).unlink(missing_ok=True)
    return {"custom_art": version}


def remove_folder_image(conn: sqlite3.Connection, images_dir: Path, library_id: int, rel_dir: str) -> dict:
    rel_dir = clean_dir(rel_dir)
    existing = custom_folder_image(conn, library_id, rel_dir)
    if existing:
        conn.execute("DELETE FROM folder_images WHERE library_id = ? AND rel_dir = ?", (library_id, rel_dir))
        conn.commit()
        folder_image_path(images_dir, existing["uid"], existing["version"]).unlink(missing_ok=True)
    return {"custom_art": None}


# ---- Housekeeping ---------------------------------------------------------------------


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
    """Delete uploaded images nothing points at any more. Returns how many.

    Only files older than the grace period go: a file that was just written may
    belong to an upload that hasn't recorded it in the database yet.
    """
    # Folder images whose folder no longer holds any videos (or whose library is gone).
    for row in conn.execute("SELECT uid, library_id, rel_dir FROM folder_images").fetchall():
        try:
            _folder(conn, row["library_id"], row["rel_dir"])
        except NotFound:
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
    """Images used to be stored as "<uuid>.jpg"; rename them to "<uuid>-<version>.jpg"."""
    rows = {
        "videos": conn.execute("SELECT uid, custom_image FROM media_items WHERE custom_image IS NOT NULL").fetchall(),
        "folders": conn.execute("SELECT uid, version FROM folder_images").fetchall(),
        "tags": conn.execute("SELECT uid, image_version FROM tags WHERE image_version IS NOT NULL").fetchall(),
    }
    for kind, pairs in rows.items():
        for uid, version in pairs:
            old = images_dir / kind / f"{uid}.jpg"
            new = images_dir / kind / upload_name(uid, version)
            if old.is_file() and not new.exists():
                old.replace(new)


def move_old_tag_images(data_dir: Path, images_dir: Path) -> None:
    """Tag images used to live in <data>/tag-images; they're now in images/tags."""
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
