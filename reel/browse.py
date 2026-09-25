"""Read-only views over the catalog: folder listings and item details.

Everything here comes from the database, so browsing never touches the NAS.
"""
import re
import sqlite3
from pathlib import PurePosixPath

SORTS = ("name", "year")


class NotFound(LookupError):
    pass


def natural_key(text: str) -> list:
    """Sort 'clip2' before 'clip10' and '0360' after '0305' (numbers compare as numbers)."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


def clean_dir(rel_dir: str | None) -> str:
    """Normalise a folder path from the URL; reject anything that climbs out."""
    parts = [p for p in (rel_dir or "").split("/") if p not in ("", ".")]
    if ".." in parts:
        raise NotFound("Folder not found.")
    return "/".join(parts)


def item_out(row: sqlite3.Row) -> dict:
    return {
        "id": row["uid"],
        "title": row["title"],
        "year": row["year"],
        "duration": row["duration"],
        "width": row["width"],
        "height": row["height"],
        "play_mode": row["play_mode"],
        "has_poster": row["poster_path"] is not None,  # an image on the NAS
        "custom_image": row["custom_image"],            # version of an uploaded one, if any
    }


def _sort_items(rows: list[sqlite3.Row], sort: str) -> list[sqlite3.Row]:
    by_name = sorted(rows, key=lambda r: (natural_key(r["title"]), natural_key(r["rel_path"])))
    if sort == "year":
        # Oldest first; videos without a year go last, in name order.
        return sorted(by_name, key=lambda r: (r["year"] is None, r["year"] or 0))
    return by_name


def breadcrumbs(library_name: str, rel_dir: str) -> list[dict]:
    crumbs = [{"name": library_name, "path": ""}]
    parts = rel_dir.split("/") if rel_dir else []
    for i, part in enumerate(parts):
        crumbs.append({"name": part, "path": "/".join(parts[: i + 1])})
    return crumbs


def browse(conn: sqlite3.Connection, library_id: int, rel_dir: str | None, sort: str = "name") -> dict:
    """List the subfolders and videos directly inside `rel_dir` of a library.

    Only folders that (somewhere below them) contain videos are listed.
    `has_art` says whether a folder has its own folder.<ext> preview.
    """
    lib = conn.execute("SELECT uid, name FROM libraries WHERE id = ?", (library_id,)).fetchone()
    if lib is None:
        raise NotFound("Library not found.")
    rel_dir = clean_dir(rel_dir)
    if sort not in SORTS:
        sort = "name"

    prefix = f"{rel_dir}/" if rel_dir else ""
    rows = conn.execute(
        # Videos a scan couldn't find any more are hidden (kept for a grace period).
        "SELECT * FROM media_items WHERE library_id = ? AND substr(rel_path, 1, ?) = ? AND missing_since IS NULL",
        (library_id, len(prefix), prefix),
    ).fetchall()
    if rel_dir and not rows:
        raise NotFound("Folder not found.")

    art = {
        row["rel_dir"]
        for row in conn.execute("SELECT rel_dir FROM folder_art WHERE library_id = ?", (library_id,))
    }
    custom_art = {
        row["rel_dir"]: row["version"]
        for row in conn.execute("SELECT rel_dir, version FROM folder_images WHERE library_id = ?", (library_id,))
    }

    items, folders = [], {}
    for row in rows:
        rest = row["rel_path"][len(prefix):]
        if "/" in rest:
            folders.setdefault(rest.split("/", 1)[0], []).append(row)
        else:
            items.append(row)

    folder_list = []
    for name in sorted(folders, key=natural_key):
        path = f"{prefix}{name}"
        folder_list.append({
            "name": name,
            "path": path,
            "item_count": len(folders[name]),
            "has_art": path in art,                 # folder.<ext> on the NAS
            "custom_art": custom_art.get(path),     # version of an uploaded image, if any
        })

    return {
        "library": {"id": lib["uid"], "name": lib["name"]},
        "path": rel_dir,
        "breadcrumbs": breadcrumbs(lib["name"], rel_dir),
        "sort": sort,
        "folders": folder_list,
        "items": [item_out(r) for r in _sort_items(items, sort)],
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
