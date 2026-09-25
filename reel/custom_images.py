"""Images you upload for folders and videos that have none on the NAS.

A NAS image (folder.png, or zombie.png beside zombie.mp4) always wins; an
uploaded image is only shown when there isn't one. Uploads are stored in the
data folder, never on the NAS:

    images/videos/<video uuid>.jpg
    images/folders/<folder image uuid>.jpg     (one row per folder in folder_images)
    images/tags/<tag uuid>.jpg                 (see tags.py)
"""
import sqlite3
from pathlib import Path

from .browse import NotFound, clean_dir
from .db import new_uid
from .images import save_upload


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


def video_image_path(images_dir: Path, item_uid: str) -> Path:
    return images_dir / "videos" / f"{item_uid}.jpg"


def set_video_image(conn: sqlite3.Connection, images_dir: Path, item_uid: str, data: bytes) -> dict:
    row = _video(conn, item_uid)
    if row["poster_path"]:
        raise ImageConflict(f"This video already has an image on the NAS ({Path(row['poster_path']).name}).")
    save_upload(data, video_image_path(images_dir, row["uid"]))
    version = _version()
    conn.execute("UPDATE media_items SET custom_image = ? WHERE id = ?", (version, row["id"]))
    conn.commit()
    return {"custom_image": version}


def remove_video_image(conn: sqlite3.Connection, images_dir: Path, item_uid: str) -> dict:
    row = _video(conn, item_uid)
    conn.execute("UPDATE media_items SET custom_image = NULL WHERE id = ?", (row["id"],))
    conn.commit()
    video_image_path(images_dir, row["uid"]).unlink(missing_ok=True)
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


def folder_image_path(images_dir: Path, image_uid: str) -> Path:
    return images_dir / "folders" / f"{image_uid}.jpg"


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
    save_upload(data, folder_image_path(images_dir, image_uid))
    version = _version()
    conn.execute(
        """
        INSERT INTO folder_images (uid, library_id, rel_dir, version) VALUES (?, ?, ?, ?)
        ON CONFLICT (library_id, rel_dir) DO UPDATE SET version = excluded.version
        """,
        (image_uid, library_id, rel_dir, version),
    )
    conn.commit()
    return {"custom_art": version}


def remove_folder_image(conn: sqlite3.Connection, images_dir: Path, library_id: int, rel_dir: str) -> dict:
    rel_dir = clean_dir(rel_dir)
    existing = custom_folder_image(conn, library_id, rel_dir)
    if existing:
        conn.execute("DELETE FROM folder_images WHERE library_id = ? AND rel_dir = ?", (library_id, rel_dir))
        conn.commit()
        folder_image_path(images_dir, existing["uid"]).unlink(missing_ok=True)
    return {"custom_art": None}


# ---- Housekeeping ---------------------------------------------------------------------


def prune(conn: sqlite3.Connection, images_dir: Path) -> int:
    """Delete uploaded images whose video, folder or tag is gone. Returns how many."""
    keep = {
        "videos": {r[0] for r in conn.execute("SELECT uid FROM media_items WHERE custom_image IS NOT NULL")},
        "folders": {r[0] for r in conn.execute("SELECT uid FROM folder_images")},
        "tags": {r[0] for r in conn.execute("SELECT uid FROM tags WHERE image_version IS NOT NULL")},
    }
    # Folder images whose folder no longer holds any videos (or whose library is gone).
    for row in conn.execute("SELECT uid, library_id, rel_dir FROM folder_images").fetchall():
        try:
            _folder(conn, row["library_id"], row["rel_dir"])
        except NotFound:
            conn.execute("DELETE FROM folder_images WHERE uid = ?", (row["uid"],))
            keep["folders"].discard(row["uid"])
    conn.commit()
    removed = 0
    for kind, uids in keep.items():
        folder = images_dir / kind
        if not folder.is_dir():
            continue
        for path in folder.glob("*.jpg"):
            if path.stem not in uids:
                path.unlink(missing_ok=True)
                removed += 1
    return removed


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
