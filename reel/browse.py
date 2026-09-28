"""Read-only views over the catalog: folder listings and item details.

Everything here comes from the database, so browsing never touches the NAS.
"""
import sqlite3
from pathlib import PurePosixPath

from .catalog import NotFound, clean_dir, descendants, page_bounds
from .plan import Capabilities, plan
from .sorting import folder_range, natural_text

SORTS = ("name", "year")


# ffprobe's format names -> what to call the file: (the usual name, {extension: a
# more exact name within that family}). ffprobe reports some formats as families
# ("mov,mp4,..." is MP4 or QuickTime), so the extension picks the member. Only the
# extension, never the name alone: a .mp4 that's really MPEG-TS is shown as TS.
FORMAT_NAMES = {
    "mov,mp4,m4a,3gp,3g2,mj2": ("MP4", {"mov": "MOV", "m4v": "M4V", "3gp": "3GP", "3g2": "3G2"}),
    "matroska,webm": ("MKV", {"webm": "WebM"}),
    "mpegts": ("TS", {}),
    "avi": ("AVI", {}),
    "mpeg": ("MPG", {"vob": "VOB"}),
    "asf": ("WMV", {"asf": "ASF"}),
}


def video_type(container: str | None, rel_path: str) -> str | None:
    """The file's real format, in plain words ("MP4", "TS", "AVI"...), from what
    ffprobe found; the extension when it couldn't read the file (None if none)."""
    name = rel_path.rsplit("/", 1)[-1]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if not container:
        return ext.upper() or None
    usual, members = FORMAT_NAMES.get(container, (container.split(",", 1)[0].upper(), {}))
    return members.get(ext, usual)


def item_out(row: sqlite3.Row, caps: Capabilities | None = None, hls_support: str = "none") -> dict:
    """A video for a list or its page. With the viewer's `caps` (and how it plays
    HLS), also `play_mode`: how that browser will play it (see plan.py), which
    colours its card, and matches what its page says."""
    out = {
        "id": row["uid"],
        "title": row["title"],
        "year": row["year"],
        "duration": row["duration"],
        "width": row["width"],
        "height": row["height"],
        "has_poster": row["poster_path"] is not None,  # an image on the NAS
        "poster_rev": row["poster_rev"],                # its version (in its thumbnail's URL)
        "custom_image": row["custom_image"],            # version of an uploaded one, if any
        "rel_path": row["rel_path"],                    # its path in the library, file name included
        "type": video_type(row["container"], row["rel_path"]),
    }
    if caps is not None:
        out["play_mode"] = plan(row, caps, hls_support).mode
    return out


ORDER_BY = {
    "name": "title_key",
    # Oldest first; videos without a year go last, in name order.
    "year": "year IS NULL, year, title_key",
}


def breadcrumbs(library_name: str, rel_dir: str) -> list[dict]:
    crumbs = [{"name": library_name, "path": ""}]
    parts = rel_dir.split("/") if rel_dir else []
    for i, part in enumerate(parts):
        crumbs.append({"name": part, "path": "/".join(parts[: i + 1])})
    return crumbs


def browse(
    conn: sqlite3.Connection,
    library_id: int,
    rel_dir: str | None,
    sort: str = "name",
    *,
    limit: int | None = None,
    offset: int | None = None,
    show_all: bool = False,
    caps: Capabilities | None = None,
    hls_support: str = "none",
) -> dict:
    """The subfolders and (one page of) videos directly inside `rel_dir` of a library.

    Both come straight from indexes, however big the library: the videos are the
    rows whose parent_dir is this folder, sorted and paged in SQL; the subfolders
    are the first path segment of every parent_dir below it, with video counts.
    Only folders that (somewhere below them) contain videos are listed.
    `has_art` says whether a folder has its own folder.<ext> preview.

    With `show_all`, it's instead every video in the folder and all its subfolders
    (and no folders), always in full-path order: see browse_all().
    `caps` / `hls_support`: the viewer's browser, for each video's play_mode (item_out).
    """
    lib = conn.execute("SELECT uid, name FROM libraries WHERE id = ?", (library_id,)).fetchone()
    if lib is None:
        raise NotFound("Library not found.")
    rel_dir = clean_dir(rel_dir)
    if sort not in SORTS:
        sort = "name"
    limit, offset = page_bounds(limit, offset)
    if show_all:
        return browse_all(conn, library_id, lib, rel_dir, limit, offset, caps, hls_support)

    # Videos a scan couldn't find any more are hidden (kept for a grace period).
    here = "library_id = ? AND parent_dir = ? AND missing_since IS NULL"
    total = conn.execute(f"SELECT COUNT(*) FROM media_items WHERE {here}", (library_id, rel_dir)).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM media_items WHERE {here} ORDER BY {ORDER_BY[sort]} LIMIT ? OFFSET ?",
        (library_id, rel_dir, limit, offset),
    ).fetchall()

    # Every folder below this one with its video count, straight from the index
    # (grouping by the stored parent_dir follows the index order), then rolled up
    # into this folder's direct children.
    low, high = descendants(rel_dir)
    skip = len(rel_dir) + 1 if rel_dir else 0
    below = conn.execute(
        f"""
        SELECT parent_dir, COUNT(*) FROM media_items
        WHERE library_id = ? AND parent_dir >= ? {"AND parent_dir < ?" if high else ""}
          AND missing_since IS NULL
        GROUP BY parent_dir
        """,
        (library_id, low, high) if high else (library_id, low),
    ).fetchall()
    child_counts: dict[str, int] = {}
    for folder, n in below:
        name = folder[skip:].split("/", 1)[0]
        child_counts[name] = child_counts.get(name, 0) + n
    counts = [{"name": name, "item_count": n} for name, n in child_counts.items()]
    if rel_dir and not total and not counts:
        raise NotFound("Folder not found.")

    prefix = f"{rel_dir}/" if rel_dir else ""
    paths = [prefix + r["name"] for r in counts]
    art, custom_art = {}, {}
    if paths:
        marks = ",".join("?" * len(paths))
        art = dict(conn.execute(
            f"SELECT rel_dir, art_rev FROM folder_art WHERE library_id = ? AND rel_dir IN ({marks})",
            (library_id, *paths)).fetchall())
        custom_art = dict(conn.execute(
            f"SELECT rel_dir, version FROM folder_images WHERE library_id = ? AND rel_dir IN ({marks})",
            (library_id, *paths)).fetchall())

    folder_list = [
        {
            "name": r["name"],
            "path": prefix + r["name"],
            "item_count": r["item_count"],
            "has_art": prefix + r["name"] in art,             # folder.<ext> on the NAS
            "art_rev": art.get(prefix + r["name"]),           # its version (in its thumbnail's URL)
            "custom_art": custom_art.get(prefix + r["name"]),  # version of an uploaded image, if any
        }
        # Same order as the videos (see sorting.py).
        for r in sorted(counts, key=lambda r: natural_text(r["name"]))
    ]

    return {
        "library": {"id": lib["uid"], "name": lib["name"]},
        "path": rel_dir,
        "breadcrumbs": breadcrumbs(lib["name"], rel_dir),
        "sort": sort,
        "all": False,
        "folders": folder_list,
        "items": [item_out(r, caps, hls_support) for r in rows],
        "total_items": total,
        "offset": offset,
        "limit": limit,
    }


def _all_below(library_id: int, rel_dir: str) -> tuple[str, list]:
    """SQL for the videos a "Show all" list of `rel_dir` holds: (WHERE clause, its args).

    They're exactly one range of the path_key index (see sorting.folder_range).
    """
    where, args = "library_id = ? AND missing_since IS NULL", [library_id]
    if rel_dir:
        where += " AND path_key >= ? AND path_key < ?"
        args += folder_range(rel_dir)
    return where, args


def browse_all(conn: sqlite3.Connection, library_id: int, lib: sqlite3.Row, rel_dir: str,
               limit: int, offset: int, caps: Capabilities | None, hls_support: str) -> dict:
    """One page of every video at or below `rel_dir`, sorted by its path in the
    library (file name included), naturally, and paged from the path_key index.
    There's no other sort: the cards show that path, so the order is the one on screen.

    Each video is followed by its clips (made on its edit page), in the order they
    were made: entries with `kind: "clip"` (see clip_out). Paging counts videos
    only (`limit` videos, each with all its clips), so a video and its clips are
    never split across pages: `next_offset` is where the next page starts, and
    `total_items` / `total_clips` count the whole list.
    """
    where, args = _all_below(library_id, rel_dir)
    total = conn.execute(f"SELECT COUNT(*) FROM media_items WHERE {where}", args).fetchone()[0]
    if rel_dir and not total:
        raise NotFound("Folder not found.")
    rows = conn.execute(
        f"SELECT * FROM media_items WHERE {where} ORDER BY path_key LIMIT ? OFFSET ?",
        (*args, limit, offset),
    ).fetchall()
    total_clips = conn.execute(
        f"SELECT COUNT(*) FROM clips WHERE item_id IN (SELECT id FROM media_items WHERE {where})", args
    ).fetchone()[0]
    clips_of: dict[int, list] = {}
    if total_clips and rows:
        marks = ",".join("?" * len(rows))
        for c in conn.execute(
            f'SELECT item_id, uid, number, start, "end" FROM clips WHERE item_id IN ({marks}) ORDER BY item_id, number',
            [r["id"] for r in rows],
        ):
            clips_of.setdefault(c["item_id"], []).append(c)
    items = []
    for r in rows:
        items.append(item_out(r, caps, hls_support))
        items += [clip_out(c, r) for c in clips_of.get(r["id"], [])]
    return {
        "library": {"id": lib["uid"], "name": lib["name"]},
        "path": rel_dir,
        "breadcrumbs": breadcrumbs(lib["name"], rel_dir),
        "all": True,
        "folders": [],
        "items": items,
        "total_items": total,
        "total_clips": total_clips,
        "offset": offset,
        "next_offset": offset + len(rows),
        "limit": limit,
    }


def clip_out(clip: sqlite3.Row, video: sqlite3.Row) -> dict:
    """A clip in a "Show all" list, right after its video: its own id, name and
    times, and its video's id, title and path."""
    return {
        "kind": "clip",
        "id": clip["uid"],
        "name": f"Clip {clip['number']}",
        "start": clip["start"],
        "end": clip["end"],
        "video_id": video["uid"],
        "title": video["title"],
        "rel_path": video["rel_path"],
    }


def neighbors(conn: sqlite3.Connection, uid: str, rel_dir: str | None, kind: str = "video") -> dict:
    """Where a video, or a clip (`kind="clip"`), is in the "Show all" list of
    `rel_dir` (in its video's library), for the prev/next buttons on its page.

    The list is the one on screen: each video followed by its clips (in the order
    made). Returns the position (from 1), the list's length (videos and clips),
    and the entries just before and after it (None at either end): a video's next
    is its first clip, if it has any; a clip's previous is its video, or the clip
    before it. Videos are one step along the path_key index. NotFound if it isn't
    in that list (any more).
    """
    number = None
    if kind == "clip":
        found = conn.execute(
            "SELECT c.number, m.uid, m.library_id FROM clips c JOIN media_items m ON m.id = c.item_id WHERE c.uid = ?",
            (uid,)).fetchone()
        if found is None:
            raise NotFound("Clip not found.")
        number, video_uid = found["number"], found["uid"]
    else:
        found = conn.execute("SELECT uid, library_id FROM media_items WHERE uid = ?", (uid,)).fetchone()
        if found is None:
            raise NotFound("Video not found.")
        video_uid = found["uid"]
    where, args = _all_below(found["library_id"], clean_dir(rel_dir))
    here = conn.execute(f"SELECT * FROM media_items WHERE {where} AND uid = ?", (*args, video_uid)).fetchone()
    if here is None:
        raise NotFound(f"That {'clip' if number is not None else 'video'} isn't in this list.")
    key = here["path_key"]

    def clips_of(video: sqlite3.Row) -> list[sqlite3.Row]:
        return conn.execute('SELECT uid, number, start, "end" FROM clips WHERE item_id = ? ORDER BY number',
                            (video["id"],)).fetchall()

    def video_entry(video: sqlite3.Row) -> dict:
        return {"kind": "video", "id": video["uid"], "title": video["title"], "rel_path": video["rel_path"]}

    def step(op: str, direction: str) -> sqlite3.Row | None:
        return conn.execute(
            f"SELECT * FROM media_items WHERE {where} AND path_key {op} ? ORDER BY path_key {direction} LIMIT 1",
            (*args, key),
        ).fetchone()

    mine = clips_of(here)
    at = next((i for i, c in enumerate(mine) if c["number"] == number), None)   # the clip's place among its video's
    if at is None:   # the video: next is its first clip; previous is the last clip of the video before
        after = clip_out(mine[0], here) if mine else None
        prev_video = step("<", "DESC")
        before_it = clips_of(prev_video) if prev_video else []
        before = clip_out(before_it[-1], prev_video) if before_it else (video_entry(prev_video) if prev_video else None)
    else:            # a clip: between its video's other clips, then its video / the next video
        after = clip_out(mine[at + 1], here) if at + 1 < len(mine) else None
        before = clip_out(mine[at - 1], here) if at > 0 else video_entry(here)
    if after is None:
        next_video = step(">", "ASC")
        after = video_entry(next_video) if next_video else None

    videos_before = conn.execute(f"SELECT COUNT(*) FROM media_items WHERE {where} AND path_key < ?", (*args, key)).fetchone()[0]
    clips_before = conn.execute(
        f"SELECT COUNT(*) FROM clips WHERE item_id IN (SELECT id FROM media_items WHERE {where} AND path_key < ?)",
        (*args, key)).fetchone()[0]
    videos = conn.execute(f"SELECT COUNT(*) FROM media_items WHERE {where}", args).fetchone()[0]
    clips = conn.execute(f"SELECT COUNT(*) FROM clips WHERE item_id IN (SELECT id FROM media_items WHERE {where})",
                         args).fetchone()[0]
    position = videos_before + clips_before + 1 + (0 if at is None else at + 1)
    return {"position": position, "total": videos + clips, "prev": before, "next": after}


def item_detail(conn: sqlite3.Connection, item_uid: str) -> dict:
    row = conn.execute(
        """
        SELECT m.*, l.name AS library_name, l.uid AS library_uid FROM media_items m
        JOIN libraries l ON l.id = m.library_id WHERE m.uid = ?
        """,
        (item_uid,),
    ).fetchone()
    if row is None:
        raise NotFound("Video not found.")
    folder = str(PurePosixPath(row["rel_path"]).parent)
    folder = "" if folder == "." else folder
    return {
        **item_out(row),
        "library_id": row["library_uid"],
        "library_name": row["library_name"],
        "rel_path": row["rel_path"],
        "folder": folder,
        "breadcrumbs": breadcrumbs(row["library_name"], folder),
        "size": row["size"],
        "container": row["container"],
        "video_codec": row["video_codec"],
        "audio_codec": row["audio_codec"],
        "pix_fmt": row["pix_fmt"],
        "interlaced": bool(row["interlaced"]),
        "probe_error": row["probe_error"],
        "missing": row["missing_since"] is not None,
    }
