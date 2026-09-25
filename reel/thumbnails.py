"""Pictures on cards: your own (served as stored) and NAS pictures (thumbnailed).

A NAS picture (a video's zombie.png, a folder's folder.<ext>) is shrunk by
ffmpeg into a small cached JPEG. The scan records each picture's version (its
size and modification time), and thumbnails are cached under it, so:

- serving a thumbnail already made touches only the catalog and the cache,
  never the NAS; the version is in the URL, so the browser may keep it for good;
- making one waits for a slot (at most SLOTS at once, one while someone is
  watching) before it takes a request thread, then re-checks the picture is
  still inside its library and runs ffmpeg;
- after scans, missing thumbnails are made in the background and old versions
  are deleted (see warm() and prune(), called by the scan manager).
"""
import asyncio
import sqlite3
from collections.abc import Callable
from pathlib import Path

from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from . import pictures
from .catalog import NotFound, clean_dir
from .db import connect
from .images import Thumbnailer, ThumbnailError
from .paths import OutsideRoot, resolve_inside

SLOTS = 4  # NAS pictures thumbnailed at once (one while someone is watching)
THUMB_HEADERS = {"Cache-Control": "private, max-age=3600"}
IMMUTABLE_HEADERS = {"Cache-Control": "private, max-age=31536000, immutable"}


class Thumbnails:
    def __init__(self, db_path: Path, media_root: Path, images_dir: Path, thumbnailer: Thumbnailer,
                 watching: Callable[[], bool]):
        self.db_path = db_path
        self.media_root = media_root
        self.images_dir = images_dir
        self.thumbnailer = thumbnailer
        self.watching = watching
        self._slots = asyncio.Semaphore(SLOTS)
        self._one = asyncio.Semaphore(1)

    # ---- Finding a card's picture (the catalog only, a short connection) ----------------

    def item_picture(self, item_uid: str) -> dict:
        """{"file", "version"} for a picture you uploaded or snapped, else {"root",
        "rel", "rev", "shape"} for the video's image on the NAS (zombie.png beside
        zombie.mp4). NotFound if it has neither: the browser shows a placeholder."""
        conn = connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT uid, library_id, poster_path, poster_rev FROM media_items WHERE uid = ?", (item_uid,)
            ).fetchone()
            if row is None:
                raise NotFound("Video not found.")
            owner = pictures.VideoPicture(row["uid"])
            uploaded = pictures.picture_file(conn, self.images_dir, owner)
            if uploaded:
                return {"file": uploaded, "version": owner.current(conn)[1]}
            if row["poster_path"]:
                return {"root": _library_root(conn, row["library_id"]), "rel": row["poster_path"],
                        "rev": row["poster_rev"], "shape": "landscape"}
            raise NotFound("No image.")
        finally:
            conn.close()

    def folder_picture(self, library_uid: str, rel_dir: str) -> dict:
        """A folder's picture, as above: yours, else its folder.<ext> on the NAS."""
        conn = connect(self.db_path)
        try:
            lib = conn.execute("SELECT id FROM libraries WHERE uid = ?", (library_uid,)).fetchone()
            if lib is None:
                raise NotFound("Library not found.")
            owner = pictures.FolderPicture(lib["id"], clean_dir(rel_dir))
            uploaded = pictures.picture_file(conn, self.images_dir, owner)
            if uploaded:
                return {"file": uploaded, "version": owner.current(conn)[1]}
            row = conn.execute(
                "SELECT art_path, art_rev FROM folder_art WHERE library_id = ? AND rel_dir = ?",
                (lib["id"], owner.rel_dir),
            ).fetchone()
            if row is not None:
                return {"root": _library_root(conn, lib["id"]), "rel": row["art_path"],
                        "rev": row["art_rev"], "shape": "poster"}
            raise NotFound("No folder art.")
        finally:
            conn.close()

    # ---- Serving -----------------------------------------------------------------------

    async def response(self, found: dict, requested: str | None) -> FileResponse:
        """The picture as a response; `requested` is the version the URL named."""
        if "file" in found:
            path, current = found["file"], found["version"]
        else:
            current = found["rev"]
            path = self.thumbnailer.cached(_key(found), current, found["shape"]) if current else None
            if path is None:
                try:
                    async with self._slots:
                        if self.watching():
                            async with self._one:
                                path = await run_in_threadpool(self.make, found)
                        else:
                            path = await run_in_threadpool(self.make, found)
                except ThumbnailError:
                    raise NotFound("No image.")
        # The URL names the version: while it's current, the browser may keep it for good.
        headers = IMMUTABLE_HEADERS if requested and requested == current else THUMB_HEADERS
        return FileResponse(path, media_type="image/jpeg", headers=headers)

    def make(self, found: dict) -> Path:
        """In a worker thread: check the NAS picture is still inside its library (it
        may have been swapped for a link since the scan), then thumbnail it."""
        try:
            resolve_inside(self.media_root, found["root"])
            src = resolve_inside(found["root"], found["rel"])
        except (OutsideRoot, OSError):
            raise ThumbnailError("The picture is missing.")
        return self.thumbnailer.make(src, found["shape"], key_src=_key(found), rev=found["rev"])

    # ---- After scans (on the scan thread) ------------------------------------------------

    def nas_pictures(self, conn: sqlite3.Connection, library_ids: list[int] | None = None) -> list[dict]:
        """Every NAS picture with a recorded version (in these libraries, or all)."""
        where, args = "", ()
        if library_ids is not None:
            where = f"AND l.id IN ({','.join('?' * len(library_ids))})"
            args = tuple(library_ids)
        rows = conn.execute(
            f"""
            SELECT l.path AS root, m.poster_path AS rel, m.poster_rev AS rev, 'landscape' AS shape
              FROM media_items m JOIN libraries l ON l.id = m.library_id
             WHERE m.poster_rev IS NOT NULL AND m.missing_since IS NULL {where}
            UNION ALL
            SELECT l.path, a.art_path, a.art_rev, 'poster'
              FROM folder_art a JOIN libraries l ON l.id = a.library_id
             WHERE a.art_rev IS NOT NULL {where}
            """,
            args * 2,
        ).fetchall()
        return [{"root": Path(r["root"]), "rel": r["rel"], "rev": r["rev"], "shape": r["shape"]} for r in rows]

    def prune(self) -> int:
        """Delete thumbnails of old picture versions, and of pictures that are gone."""
        conn = connect(self.db_path)
        try:
            keep = {self.thumbnailer.path_for(_key(p), p["rev"], p["shape"]) for p in self.nas_pictures(conn)}
        finally:
            conn.close()
        return self.thumbnailer.prune(keep)

    def warm(self, library_ids: list[int], should_stop: Callable[[], bool]) -> bool:
        """Make the missing thumbnails of these libraries, one at a time, so a first
        look at a folder doesn't wait for ffmpeg. Checks `should_stop` (a scan
        queued, someone watching, Reel stopping) before each one. Returns whether
        it finished; if not, the rest are made on first view or after the next scan."""
        if not library_ids:
            return True
        conn = connect(self.db_path)
        try:
            todo = [p for p in self.nas_pictures(conn, library_ids)
                    if self.thumbnailer.cached(_key(p), p["rev"], p["shape"]) is None]
        finally:
            conn.close()
        for found in todo:
            if should_stop():
                return False
            try:
                self.make(found)
            except ThumbnailError:
                pass  # a broken picture (or Reel stopping): the browser shows the placeholder
        return True


def _key(found: dict) -> str:
    """The picture's path as the catalog knows it: part of its thumbnail's cache key."""
    return str(found["root"] / found["rel"])


def _library_root(conn: sqlite3.Connection, library_id: int) -> Path:
    row = conn.execute("SELECT path FROM libraries WHERE id = ?", (library_id,)).fetchone()
    if row is None:
        raise NotFound("Library not found.")
    return Path(row["path"])
