"""Ranges: a stretch of a video (a start and an end), marked on its edit page.

Nothing is ever cut from the file itself (Reel never writes to the videos).
What a range does is its `kind`:

- range: just marked, to find again;
- skip:  jumped over when the video plays (the "deleted" part);
- clip:  shown on the video's page as a video of its own, which plays only
         that stretch.

Like marks, ranges belong to the video's catalog row, so they follow a moved
or renamed file and go when the video is removed from the catalog.
"""
import sqlite3

from .catalog import NotFound
from .db import new_uid

KINDS = ("range", "skip", "clip")
MIN_LENGTH = 0.5    # seconds
MAX_LABEL = 100


class RangeError(Exception):
    """A range that can't be saved as asked (the message says why)."""


def _video(conn: sqlite3.Connection, item_uid: str) -> sqlite3.Row:
    row = conn.execute("SELECT id, duration FROM media_items WHERE uid = ?", (item_uid,)).fetchone()
    if row is None:
        raise NotFound("Video not found.")
    return row


def range_out(row: sqlite3.Row) -> dict:
    return {"id": row["uid"], "start": row["start"], "end": row["end"], "kind": row["kind"], "label": row["label"]}


def list_ranges(conn: sqlite3.Connection, item_uid: str) -> list[dict]:
    """The video's ranges, earliest first."""
    video = _video(conn, item_uid)
    rows = conn.execute('SELECT * FROM ranges WHERE item_id = ? ORDER BY start, "end", id', (video["id"],))
    return [range_out(r) for r in rows]


def _checked(video: sqlite3.Row, start: float, end: float, kind: str, label: str) -> tuple:
    """The values to store (times to a tenth of a second, kept inside the video),
    or RangeError."""
    if kind not in KINDS:
        raise RangeError(f"A range is one of: {', '.join(KINDS)}.")
    label = " ".join(label.split())
    if len(label) > MAX_LABEL:
        raise RangeError(f"Labels can be at most {MAX_LABEL} characters.")
    start, end = round(max(0.0, start), 1), round(max(0.0, end), 1)
    if video["duration"]:
        if start >= video["duration"]:
            raise RangeError("The start is past the end of the video.")
        end = min(end, round(video["duration"], 1))
    if end - start < MIN_LENGTH:
        raise RangeError(f"The end must be at least {MIN_LENGTH:g} seconds after the start.")
    return start, end, kind, label


def add_range(conn: sqlite3.Connection, item_uid: str, start: float, end: float, kind: str = "range",
              label: str = "") -> list[dict]:
    video = _video(conn, item_uid)
    values = _checked(video, start, end, kind, label)
    conn.execute('INSERT INTO ranges (uid, item_id, start, "end", kind, label) VALUES (?, ?, ?, ?, ?, ?)',
                 (new_uid(), video["id"], *values))
    conn.commit()
    return list_ranges(conn, item_uid)


def update_range(conn: sqlite3.Connection, item_uid: str, range_uid: str, start: float, end: float,
                 kind: str = "range", label: str = "") -> list[dict]:
    video = _video(conn, item_uid)
    values = _checked(video, start, end, kind, label)
    changed = conn.execute('UPDATE ranges SET start = ?, "end" = ?, kind = ?, label = ? WHERE uid = ? AND item_id = ?',
                           (*values, range_uid, video["id"])).rowcount
    conn.commit()
    if not changed:
        raise NotFound("Range not found.")
    return list_ranges(conn, item_uid)


def remove_range(conn: sqlite3.Connection, item_uid: str, range_uid: str) -> list[dict]:
    video = _video(conn, item_uid)
    gone = conn.execute("DELETE FROM ranges WHERE uid = ? AND item_id = ?", (range_uid, video["id"])).rowcount
    conn.commit()
    if not gone:
        raise NotFound("Range not found.")
    return list_ranges(conn, item_uid)
