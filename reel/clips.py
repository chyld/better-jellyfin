"""Clips: a stretch of a video, made on its edit page from two marks.

A clip is a start and an end in the video; nothing is cut from the file (Reel
never writes to the videos). It's listed on the video's page and its edit page
as "Clip 1", "Clip 2", ... (numbered in the order they were made; a deleted
clip's number isn't reused while later ones exist) and plays just that stretch.

Clips belong to the video's catalog row, so they follow a moved or renamed file
(like marks and tags) and go when the video is removed from the catalog.
"""
import sqlite3

from .catalog import NotFound, video_rev
from .db import new_uid

MIN_LENGTH = 0.5    # seconds


class ClipError(Exception):
    """A clip that can't be made as asked (the message says why)."""


def _video(conn: sqlite3.Connection, item_uid: str) -> sqlite3.Row:
    row = conn.execute("SELECT id, duration, size, mtime FROM media_items WHERE uid = ?", (item_uid,)).fetchone()
    if row is None:
        raise NotFound("Video not found.")
    return row


def list_clips(conn: sqlite3.Connection, item_uid: str) -> list[dict]:
    """The video's clips, in the order they were made."""
    video = _video(conn, item_uid)
    rows = conn.execute('SELECT uid, number, start, "end" FROM clips WHERE item_id = ? ORDER BY number',
                        (video["id"],))
    return [clip_fields(r, video) for r in rows]


def clip_fields(clip: sqlite3.Row, video: sqlite3.Row) -> dict:
    """What every view of a clip says: its id, name and times, and `picture`, its
    picture's version (for /api/clips/{id}/thumb?v=)."""
    return {"id": clip["uid"], "number": clip["number"], "name": f"Clip {clip['number']}",
            "start": clip["start"], "end": clip["end"], "picture": video_rev(video["size"], video["mtime"])}


def add_clip(conn: sqlite3.Connection, item_uid: str, a: float, b: float) -> list[dict]:
    """A clip between two marks, in either order (to a tenth of a second, kept
    inside the video). Returns the video's clips."""
    video = _video(conn, item_uid)
    start, end = sorted(round(max(0.0, t), 1) for t in (a, b))
    if video["duration"]:
        if start >= video["duration"]:
            raise ClipError("The clip starts after the end of the video.")
        end = min(end, round(video["duration"], 1))
    if end - start < MIN_LENGTH:
        raise ClipError(f"The marks must be at least {MIN_LENGTH:g} seconds apart.")
    # The next number is worked out in the insert itself, so two clips made at
    # once can't both get it (and the index refuses a duplicate anyway).
    conn.execute(
        'INSERT INTO clips (uid, item_id, number, start, "end") '
        "SELECT ?, ?, COALESCE(MAX(number), 0) + 1, ?, ? FROM clips WHERE item_id = ?",
        (new_uid(), video["id"], start, end, video["id"]),
    )
    conn.commit()
    return list_clips(conn, item_uid)


def remove_clip(conn: sqlite3.Connection, item_uid: str, clip_uid: str) -> list[dict]:
    video = _video(conn, item_uid)
    gone = conn.execute("DELETE FROM clips WHERE uid = ? AND item_id = ?", (clip_uid, video["id"])).rowcount
    conn.commit()
    if not gone:
        raise NotFound("Clip not found.")
    return list_clips(conn, item_uid)
