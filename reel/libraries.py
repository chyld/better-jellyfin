"""Adding, renaming and removing libraries, plus the folder picker."""
import os
import sqlite3
from pathlib import Path

from .db import new_uid


class LibraryError(ValueError):
    """A library request was rejected; the message is shown to the user."""


def resolve_in_root(media_root: Path, path: str) -> Path:
    """Resolve `path` and make sure it stays inside the media root."""
    if not path:
        raise LibraryError("Choose a folder.")
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(media_root):
        raise LibraryError(f"Folder must be inside {media_root}.")
    return resolved


def list_subfolders(media_root: Path, path: str | None) -> dict:
    """Folder picker: the subfolders of `path` (default: the media root)."""
    folder = resolve_in_root(media_root, path) if path else media_root
    if not folder.is_dir():
        raise LibraryError("Folder not found.")
    try:
        entries = sorted(
            (e for e in os.scandir(folder) if e.is_dir() and not e.name.startswith(".")),
            key=lambda e: e.name.lower(),
        )
    except PermissionError:
        raise LibraryError("Folder is not readable.")
    return {
        "path": str(folder),
        "parent": str(folder.parent) if folder != media_root else None,
        "folders": [{"name": e.name, "path": str(folder / e.name)} for e in entries],
    }


def _clean_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise LibraryError("Give the library a name.")
    if len(name) > 100:
        raise LibraryError("Name is too long (100 characters max).")
    return name


def _check_name_free(conn: sqlite3.Connection, name: str, exclude_id: int | None = None) -> None:
    row = conn.execute(
        "SELECT id FROM libraries WHERE name = ? COLLATE NOCASE AND id IS NOT ?",
        (name, exclude_id),
    ).fetchone()
    if row:
        raise LibraryError(f'A library named "{name}" already exists.')


def validate_folder(conn: sqlite3.Connection, media_root: Path, path: str) -> Path:
    folder = resolve_in_root(media_root, path)
    if not folder.is_dir():
        raise LibraryError("Folder not found.")
    if not os.access(folder, os.R_OK | os.X_OK):
        raise LibraryError("Folder is not readable.")
    for row in conn.execute("SELECT name, path FROM libraries"):
        other = Path(row["path"])
        if folder == other:
            raise LibraryError(f'This folder is already the "{row["name"]}" library.')
        if folder.is_relative_to(other):
            raise LibraryError(f'This folder is inside the "{row["name"]}" library.')
        if other.is_relative_to(folder):
            raise LibraryError(f'This folder contains the "{row["name"]}" library.')
    return folder


def create_library(conn: sqlite3.Connection, media_root: Path, name: str, path: str) -> int:
    name = _clean_name(name)
    _check_name_free(conn, name)
    folder = validate_folder(conn, media_root, path)
    cur = conn.execute(
        "INSERT INTO libraries (uid, name, path) VALUES (?, ?, ?)", (new_uid(), name, str(folder))
    )
    conn.commit()
    return cur.lastrowid


def rename_library(conn: sqlite3.Connection, library_id: int, name: str) -> None:
    name = _clean_name(name)
    _check_name_free(conn, name, exclude_id=library_id)
    cur = conn.execute("UPDATE libraries SET name = ? WHERE id = ?", (name, library_id))
    conn.commit()
    if cur.rowcount == 0:
        raise KeyError(library_id)


def delete_library(conn: sqlite3.Connection, library_id: int) -> None:
    """Forget a library and its catalog. Files on disk are never touched."""
    cur = conn.execute("DELETE FROM libraries WHERE id = ?", (library_id,))
    conn.commit()
    if cur.rowcount == 0:
        raise KeyError(library_id)


def get_library(conn: sqlite3.Connection, library_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM libraries WHERE id = ?", (library_id,)).fetchone()


def library_pk(conn: sqlite3.Connection, uid: str) -> int | None:
    """The internal id of the library with this UUID."""
    row = conn.execute("SELECT id FROM libraries WHERE uid = ?", (uid,)).fetchone()
    return row["id"] if row else None


def list_libraries(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT l.*, COUNT(m.id) AS item_count,
            EXISTS (SELECT 1 FROM folder_art a WHERE a.library_id = l.id AND a.rel_dir = '') AS has_art,
            (SELECT version FROM folder_images f WHERE f.library_id = l.id AND f.rel_dir = '') AS custom_art
        FROM libraries l LEFT JOIN media_items m ON m.library_id = l.id
        GROUP BY l.id ORDER BY l.name COLLATE NOCASE
        """
    ).fetchall()
    return [{**dict(r), "has_art": bool(r["has_art"])} for r in rows]
