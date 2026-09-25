"""Tags: free-form labels on videos. A video can have any number of them."""
import re
import sqlite3
from pathlib import Path

from .browse import NotFound, item_out, page_bounds
from .db import NOW_MS, new_uid
from .images import ThumbnailError, save_upload, upload_name

MAX_TAG_LENGTH = 50
# Tags are lowercase letters, digits and dashes only: "family", "1990s", "road-trip".
TAG_PATTERN = re.compile(r"^[a-z0-9-]+$")


class TagError(ValueError):
    """A tag request was rejected; the message is shown to the user."""


def clean_name(name: str) -> str:
    """Check a tag only uses lowercase a-z, 0-9 and dashes. Nothing is corrected."""
    name = (name or "").strip()
    if not name:
        raise TagError("Type a tag name.")
    if len(name) > MAX_TAG_LENGTH:
        raise TagError(f"Tags can be at most {MAX_TAG_LENGTH} characters.")
    if not TAG_PATTERN.match(name):
        raise TagError(f'"{name}" isn\'t a valid tag: use only lowercase a-z, 0-9 and dashes (-), with no spaces.')
    return name


def item_pk(conn: sqlite3.Connection, item_uid: str) -> int:
    row = conn.execute("SELECT id FROM media_items WHERE uid = ?", (item_uid,)).fetchone()
    if row is None:
        raise NotFound("Video not found.")
    return row["id"]


def _tag_out(row: sqlite3.Row) -> dict:
    out = {"id": row["uid"], "name": row["name"]}
    if "image_version" in row.keys():
        # Changes with every upload, so the browser never shows a stale picture.
        out["image"] = row["image_version"]
    return out


def item_tags(conn: sqlite3.Connection, item_id: int) -> list[dict]:
    """A video's tags, in the order they were added."""
    rows = conn.execute(
        """
        SELECT t.uid, t.name FROM item_tags it JOIN tags t ON t.id = it.tag_id
        WHERE it.item_id = ? ORDER BY it.added_at, it.rowid
        """,
        (item_id,),
    )
    return [_tag_out(r) for r in rows]


def add_tag(conn: sqlite3.Connection, item_uid: str, name: str) -> list[dict]:
    """Tag a video, creating the tag if it's new."""
    item_id = item_pk(conn, item_uid)
    name = clean_name(name)
    row = conn.execute("SELECT id FROM tags WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    if row:
        tag_id = row["id"]
    else:
        tag_id = conn.execute(
            "INSERT INTO tags (uid, name) VALUES (?, ?)", (new_uid(), name)
        ).lastrowid
    conn.execute(
        f"INSERT OR IGNORE INTO item_tags (item_id, tag_id, added_at) VALUES (?, ?, {NOW_MS})", (item_id, tag_id)
    )
    conn.commit()
    return item_tags(conn, item_id)


def remove_tag(conn: sqlite3.Connection, item_uid: str, tag_uid: str) -> list[dict]:
    """Untag a video. A tag left with no videos is deleted."""
    item_id = item_pk(conn, item_uid)
    tag = find_tag(conn, tag_uid)
    conn.execute("DELETE FROM item_tags WHERE item_id = ? AND tag_id = ?", (item_id, tag["id"]))
    _delete_unused(conn)
    conn.commit()
    return item_tags(conn, item_id)


def _delete_unused(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM item_tags)")


def find_tag(conn: sqlite3.Connection, tag_uid: str) -> sqlite3.Row:
    tag = conn.execute("SELECT id, uid, name, image_version FROM tags WHERE uid = ?", (tag_uid,)).fetchone()
    if tag is None:
        raise NotFound("Tag not found.")
    return tag


def _tag_with_count(conn: sqlite3.Connection, tag_id: int) -> dict:
    row = conn.execute(
        """
        SELECT t.uid, t.name, t.image_version, (SELECT COUNT(*) FROM item_tags WHERE tag_id = t.id) AS count
        FROM tags t WHERE t.id = ?
        """,
        (tag_id,),
    ).fetchone()
    return {**_tag_out(row), "count": row["count"]}


def rename_tag(conn: sqlite3.Connection, tag_uid: str, name: str, images_dir: Path) -> dict:
    """Rename a tag. Renaming it to a tag that already exists merges the two.

    Returns the resulting tag, with `merged` saying whether it was merged (its
    id is then the other tag's; the renamed one is gone).
    """
    tag = find_tag(conn, tag_uid)
    name = clean_name(name)
    other = conn.execute(
        "SELECT id, uid, image_version FROM tags WHERE name = ? AND id != ?", (name, tag["id"])
    ).fetchone()
    if other:
        # The surviving tag keeps its own image, or takes this one's if it has none.
        own = image_path(images_dir, tag["uid"], tag["image_version"]) if tag["image_version"] else None
        if own and not other["image_version"]:
            own.replace(image_path(images_dir, other["uid"], tag["image_version"]))
            conn.execute("UPDATE tags SET image_version = ? WHERE id = ?", (tag["image_version"], other["id"]))
        elif own:
            own.unlink(missing_ok=True)
        # Move every video over (keeping their order), then drop this tag.
        conn.execute(
            """
            INSERT OR IGNORE INTO item_tags (item_id, tag_id, added_at)
            SELECT item_id, ?, added_at FROM item_tags WHERE tag_id = ? ORDER BY added_at, rowid
            """,
            (other["id"], tag["id"]),
        )
        conn.execute("DELETE FROM tags WHERE id = ?", (tag["id"],))
        conn.commit()
        return {**_tag_with_count(conn, other["id"]), "merged": True}
    conn.execute("UPDATE tags SET name = ? WHERE id = ?", (name, tag["id"]))
    conn.commit()
    return {**_tag_with_count(conn, tag["id"]), "merged": False}


def delete_tag(conn: sqlite3.Connection, tag_uid: str, images_dir: Path) -> None:
    """Delete a tag, removing it from every video. The videos are untouched."""
    tag = find_tag(conn, tag_uid)
    conn.execute("DELETE FROM tags WHERE id = ?", (tag["id"],))
    conn.commit()
    if tag["image_version"]:
        image_path(images_dir, tag["uid"], tag["image_version"]).unlink(missing_ok=True)


# ---- Tag images ---------------------------------------------------------------------


def image_path(images_dir: Path, tag_uid: str, version: str) -> Path:
    return images_dir / upload_name(tag_uid, version)


def set_image(conn: sqlite3.Connection, images_dir: Path, tag_uid: str, data: bytes) -> dict:
    """Give a tag an uploaded image (stored as a JPEG), replacing any old one.

    Write the new file, record it, then delete the old one (see images.py).
    """
    tag = find_tag(conn, tag_uid)
    version = new_uid()[:8]
    try:
        save_upload(data, image_path(images_dir, tag["uid"], version))
    except ThumbnailError as exc:
        raise TagError(str(exc))
    conn.execute("UPDATE tags SET image_version = ? WHERE id = ?", (version, tag["id"]))
    conn.commit()
    if tag["image_version"]:
        image_path(images_dir, tag["uid"], tag["image_version"]).unlink(missing_ok=True)
    return _tag_with_count(conn, tag["id"])


def remove_image(conn: sqlite3.Connection, images_dir: Path, tag_uid: str) -> dict:
    tag = find_tag(conn, tag_uid)
    conn.execute("UPDATE tags SET image_version = NULL WHERE id = ?", (tag["id"],))
    conn.commit()
    if tag["image_version"]:
        image_path(images_dir, tag["uid"], tag["image_version"]).unlink(missing_ok=True)
    return _tag_with_count(conn, tag["id"])


def tag_image_file(conn: sqlite3.Connection, images_dir: Path, tag_uid: str) -> Path:
    tag = find_tag(conn, tag_uid)
    if not tag["image_version"]:
        raise NotFound("This tag has no image.")
    path = image_path(images_dir, tag["uid"], tag["image_version"])
    if not path.is_file():
        raise NotFound("This tag has no image.")
    return path


def list_tags(conn: sqlite3.Connection) -> list[dict]:
    """Every tag that is on at least one video, with how many videos have it."""
    rows = conn.execute(
        """
        SELECT t.uid, t.name, t.image_version, COUNT(*) AS count
        FROM tags t JOIN item_tags it ON it.tag_id = t.id
        JOIN media_items m ON m.id = it.item_id AND m.missing_since IS NULL
        GROUP BY t.id ORDER BY t.name COLLATE NOCASE
        """
    )
    return [{**_tag_out(r), "count": r["count"]} for r in rows]


def tag_videos(conn: sqlite3.Connection, tag_uid: str, *, limit: int | None = None, offset: int | None = None) -> dict:
    """A tag and (one page of) its videos, in the order they were tagged (no sorting yet)."""
    tag = find_tag(conn, tag_uid)
    limit, offset = page_bounds(limit, offset)
    joined = """
        FROM item_tags it JOIN media_items m ON m.id = it.item_id
        WHERE it.tag_id = ? AND m.missing_since IS NULL
    """
    total = conn.execute(f"SELECT COUNT(*) {joined}", (tag["id"],)).fetchone()[0]
    rows = conn.execute(
        f"SELECT m.* {joined} ORDER BY it.added_at, it.rowid LIMIT ? OFFSET ?", (tag["id"], limit, offset)
    )
    return {"tag": _tag_out(tag), "items": [item_out(r) for r in rows],
            "total_items": total, "offset": offset, "limit": limit}
