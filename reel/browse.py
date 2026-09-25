"""Read-only views over the catalog: folder listings and item details.

Everything here comes from the database, so browsing never touches the NAS.
"""
import sqlite3
from pathlib import PurePosixPath

from .catalog import NotFound, clean_dir, descendants, page_bounds
from .sorting import natural_text

SORTS = ("name", "year")


def item_out(row: sqlite3.Row) -> dict:
    return {
        "id": row["uid"],
        "title": row["title"],
        "year": row["year"],
        "duration": row["duration"],
        "width": row["width"],
        "height": row["height"],
        "has_poster": row["poster_path"] is not None,  # an image on the NAS
        "custom_image": row["custom_image"],            # version of an uploaded one, if any
    }


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
) -> dict:
    """The subfolders and (one page of) videos directly inside `rel_dir` of a library.

    Both come straight from indexes, however big the library: the videos are the
    rows whose parent_dir is this folder, sorted and paged in SQL; the subfolders
    are the first path segment of every parent_dir below it, with video counts.
    Only folders that (somewhere below them) contain videos are listed.
    `has_art` says whether a folder has its own folder.<ext> preview.
    """
    lib = conn.execute("SELECT uid, name FROM libraries WHERE id = ?", (library_id,)).fetchone()
    if lib is None:
        raise NotFound("Library not found.")
    rel_dir = clean_dir(rel_dir)
    if sort not in SORTS:
        sort = "name"
    limit, offset = page_bounds(limit, offset)

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
    art, custom_art = set(), {}
    if paths:
        marks = ",".join("?" * len(paths))
        art = {r[0] for r in conn.execute(
            f"SELECT rel_dir FROM folder_art WHERE library_id = ? AND rel_dir IN ({marks})", (library_id, *paths))}
        custom_art = dict(conn.execute(
            f"SELECT rel_dir, version FROM folder_images WHERE library_id = ? AND rel_dir IN ({marks})",
            (library_id, *paths)).fetchall())

    folder_list = [
        {
            "name": r["name"],
            "path": prefix + r["name"],
            "item_count": r["item_count"],
            "has_art": prefix + r["name"] in art,             # folder.<ext> on the NAS
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
        "folders": folder_list,
        "items": [item_out(r) for r in rows],
        "total_items": total,
        "offset": offset,
        "limit": limit,
    }


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
