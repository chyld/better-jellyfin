"""Marks: spots in a video you saved in the player, to jump back to later.

Just a time (no name). A video can have any number, or none. They belong to the
video's catalog row, so they follow a moved or renamed file (like tags) and go
when the video is removed from the catalog.
"""
import sqlite3

from .catalog import NotFound
from .db import new_uid

# Marking within this many seconds of an existing mark doesn't add another.
SAME_SPOT = 1.0


def _video(conn: sqlite3.Connection, item_uid: str) -> sqlite3.Row:
    row = conn.execute("SELECT id, duration FROM media_items WHERE uid = ?", (item_uid,)).fetchone()
    if row is None:
        raise NotFound("Video not found.")
    return row


def list_marks(conn: sqlite3.Connection, item_uid: str) -> list[dict]:
    """The video's marks, earliest first."""
    video = _video(conn, item_uid)
    rows = conn.execute("SELECT uid, seconds FROM marks WHERE item_id = ? ORDER BY seconds", (video["id"],))
    return [{"id": r["uid"], "time": r["seconds"]} for r in rows]


def add_mark(conn: sqlite3.Connection, item_uid: str, seconds: float) -> list[dict]:
    """Mark `seconds` into the video (kept inside it; nothing new if a mark is
    already within SAME_SPOT). Returns the video's marks."""
    video = _video(conn, item_uid)
    if video["duration"]:
        seconds = min(seconds, video["duration"])
    seconds = round(max(0.0, seconds), 1)
    near = conn.execute(
        "SELECT 1 FROM marks WHERE item_id = ? AND abs(seconds - ?) < ?", (video["id"], seconds, SAME_SPOT)
    ).fetchone()
    if near is None:
        conn.execute("INSERT INTO marks (uid, item_id, seconds) VALUES (?, ?, ?)", (new_uid(), video["id"], seconds))
        conn.commit()
    return list_marks(conn, item_uid)


def remove_mark(conn: sqlite3.Connection, item_uid: str, mark_uid: str) -> list[dict]:
    video = _video(conn, item_uid)
    gone = conn.execute("DELETE FROM marks WHERE uid = ? AND item_id = ?", (mark_uid, video["id"])).rowcount
    conn.commit()
    if not gone:
        raise NotFound("Mark not found.")
    return list_marks(conn, item_uid)
