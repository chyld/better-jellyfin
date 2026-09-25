import logging
import shutil
import sqlite3
import subprocess
from collections.abc import Iterator
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import browse, custom_images, fetch, libraries, playback, tags, users
from .paths import OutsideRoot, resolve_inside
from .config import Settings
from .db import connect, init_db
from .images import MAX_UPLOAD_BYTES, Thumbnailer, ThumbnailError
from .scan_manager import ScanManager
from .scanner import scan_library

STATIC_DIR = Path(__file__).parent / "static"


class AppFiles(StaticFiles):
    """The frontend's files, which browsers must re-check on every load.

    Without this, browsers guess a cache lifetime and can keep running an old
    script after an update. Re-checking is a cheap 304 when nothing changed.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response

# Thumbnails change only when their source does (the cache key includes its mtime).
THUMB_HEADERS = {"Cache-Control": "private, max-age=3600"}


class LibraryCreate(BaseModel):
    name: str
    path: str


class LibraryRename(BaseModel):
    name: str


class TagAdd(BaseModel):
    name: str


class TagRename(BaseModel):
    name: str


class ImageUrl(BaseModel):
    url: str


def create_app(settings: Settings | None = None, scan_manager: ScanManager | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.check_data_dir()
    init_db(settings.db_path)
    scans = scan_manager or ScanManager(
        settings.db_path,
        workers=settings.probe_workers,
        scan_fn=partial(scan_library, missing_grace=settings.missing_grace),
    )
    thumbs = Thumbnailer(settings.thumbs_dir)
    streams = playback.StreamManager(playback.StreamLimits(max_streams=settings.max_streams))
    # Uploaded images whose video, folder or tag is gone are cleaned up at
    # startup and after every scan.
    custom_images.move_old_tag_images(settings.data_dir, settings.images_dir)
    startup_conn = connect(settings.db_path)
    try:
        custom_images.adopt_unversioned_files(startup_conn, settings.images_dir)
        custom_images.prune(startup_conn, settings.images_dir)
    finally:
        startup_conn.close()
    scans.after_scan = lambda conn: custom_images.prune(conn, settings.images_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        scans.start()
        yield
        await streams.shutdown()
        scans.stop()

    app = FastAPI(title="Reel", lifespan=lifespan)
    # Docker's health check polls /api/health; keep it out of the access log.
    logging.getLogger("uvicorn.access").addFilter(lambda record: "/api/health" not in record.getMessage())
    app.state.settings = settings
    app.state.scans = scans
    app.state.streams = streams

    def get_db() -> Iterator[sqlite3.Connection]:
        conn = connect(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def library_out(row: dict) -> dict:
        # The API only ever shows the UUID; the integer id stays internal.
        out = {**row, "id": row["uid"], "scan": scans.status(row["id"])}
        del out["uid"]
        return out

    def one_library(conn: sqlite3.Connection, library_id: int) -> dict:
        return library_out(next(r for r in libraries.list_libraries(conn) if r["id"] == library_id))

    def library_pk(conn: sqlite3.Connection, library_uid: str) -> int:
        library_id = libraries.library_pk(conn, library_uid)
        if library_id is None:
            raise HTTPException(404, "Library not found.")
        return library_id

    @app.exception_handler(libraries.LibraryError)
    async def library_error(request: Request, exc: libraries.LibraryError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(tags.TagError)
    async def tag_error(request: Request, exc: tags.TagError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(custom_images.ImageConflict)
    async def image_conflict(request: Request, exc: custom_images.ImageConflict):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ThumbnailError)
    async def bad_image(request: Request, exc: ThumbnailError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(browse.NotFound)
    async def not_found(request: Request, exc: browse.NotFound):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    async def read_upload(request: Request) -> bytes:
        """The request body of an image upload, refusing anything over the limit."""
        data = bytearray()
        async for chunk in request.stream():
            data += chunk
            if len(data) > MAX_UPLOAD_BYTES:
                raise HTTPException(413, f"Images can be at most {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
        if not data:
            raise HTTPException(400, "Choose an image to upload.")
        return bytes(data)

    def download(url: str) -> bytes:
        try:
            return fetch.fetch_image_bytes(url, policy=settings.image_urls)
        except fetch.FetchError as exc:
            raise HTTPException(400, str(exc))

    def library_root(conn: sqlite3.Connection, library_id: int) -> Path:
        lib = libraries.get_library(conn, library_id)
        if lib is None:
            raise browse.NotFound("Library not found.")
        return Path(lib["path"])

    def library_file(conn: sqlite3.Connection, library_id: int, rel_path: str) -> Path:
        """A file in a library, re-checked now: it and its library must still be
        inside the media root, even if something was swapped for a symlink since
        the scan. Anything else is reported as not found."""
        root = library_root(conn, library_id)
        try:
            resolve_inside(settings.media_root, root)
            return resolve_inside(root, rel_path)
        except (OutsideRoot, OSError):
            raise HTTPException(404, "The video file is missing. Is the NAS connected?")

    def thumb_response(make) -> FileResponse:
        try:
            return FileResponse(make(), media_type="image/jpeg", headers=THUMB_HEADERS)
        except ThumbnailError:
            raise HTTPException(404, "No image.")

    def item_thumb(conn: sqlite3.Connection, item_uid: str) -> FileResponse:
        """The video's own preview (zombie.png beside zombie.mp4), shrunk.

        Videos without one get a placeholder in the browser, so this is a 404.
        """
        row = conn.execute(
            "SELECT uid, library_id, poster_path, custom_image FROM media_items WHERE uid = ?", (item_uid,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Video not found.")
        if row["poster_path"]:
            try:
                poster = library_file(conn, row["library_id"], row["poster_path"])
            except HTTPException:
                raise HTTPException(404, "No image.")
            return thumb_response(lambda: thumbs.from_image(poster, "landscape"))
        uploaded = (
            custom_images.video_image_path(settings.images_dir, row["uid"], row["custom_image"])
            if row["custom_image"] else None
        )
        if uploaded and uploaded.is_file():
            return FileResponse(uploaded, media_type="image/jpeg", headers=THUMB_HEADERS)
        raise HTTPException(404, "No image.")

    def folder_art(conn: sqlite3.Connection, library_id: int, rel_dir: str) -> FileResponse:
        row = conn.execute(
            "SELECT art_path FROM folder_art WHERE library_id = ? AND rel_dir = ?", (library_id, rel_dir)
        ).fetchone()
        if row is not None:
            try:
                art = library_file(conn, library_id, row["art_path"])
            except HTTPException:
                raise HTTPException(404, "No folder art.")
            return thumb_response(lambda: thumbs.from_image(art))
        custom = custom_images.custom_folder_image(conn, library_id, rel_dir)
        if custom:
            uploaded = custom_images.folder_image_path(settings.images_dir, custom["uid"], custom["version"])
            if uploaded.is_file():
                return FileResponse(uploaded, media_type="image/jpeg", headers=THUMB_HEADERS)
        raise HTTPException(404, "No folder art.")

    @app.get("/api/libraries/{library_uid}/browse")
    def browse_folder(library_uid: str, path: str = "", sort: str = "name", conn: sqlite3.Connection = Depends(get_db)):
        return browse.browse(conn, library_pk(conn, library_uid), path, sort)

    @app.get("/api/libraries/{library_uid}/folder-art")
    def get_folder_art(library_uid: str, path: str = "", conn: sqlite3.Connection = Depends(get_db)):
        return folder_art(conn, library_pk(conn, library_uid), browse.clean_dir(path))

    @app.get("/api/items/{item_uid}")
    def get_item(item_uid: str, conn: sqlite3.Connection = Depends(get_db)):
        item = browse.item_detail(conn, item_uid)
        return {**item, "tags": tags.item_tags(conn, tags.item_pk(conn, item_uid))}

    @app.post("/api/items/{item_uid}/tags")
    def add_item_tag(item_uid: str, body: TagAdd, conn: sqlite3.Connection = Depends(get_db)):
        """Tag a video; returns the video's tags."""
        return tags.add_tag(conn, item_uid, body.name)

    @app.delete("/api/items/{item_uid}/tags/{tag_uid}")
    def remove_item_tag(item_uid: str, tag_uid: str, conn: sqlite3.Connection = Depends(get_db)):
        """Untag a video; returns the video's remaining tags."""
        return tags.remove_tag(conn, item_uid, tag_uid)

    @app.get("/api/tags")
    def list_tags(conn: sqlite3.Connection = Depends(get_db)):
        return tags.list_tags(conn)

    @app.get("/api/tags/{tag_uid}")
    def get_tag(tag_uid: str, conn: sqlite3.Connection = Depends(get_db)):
        return tags.tag_videos(conn, tag_uid)

    @app.patch("/api/tags/{tag_uid}")
    def rename_tag(tag_uid: str, body: TagRename, conn: sqlite3.Connection = Depends(get_db)):
        """Rename a tag; renaming it to an existing tag merges the two."""
        return tags.rename_tag(conn, tag_uid, body.name, settings.tag_images_dir)

    @app.delete("/api/tags/{tag_uid}", status_code=204)
    def delete_tag(tag_uid: str, conn: sqlite3.Connection = Depends(get_db)):
        """Delete a tag from every video."""
        tags.delete_tag(conn, tag_uid, settings.tag_images_dir)
        return Response(status_code=204)

    @app.put("/api/tags/{tag_uid}/image")
    async def upload_tag_image(tag_uid: str, request: Request, conn: sqlite3.Connection = Depends(get_db)):
        """Set a tag's image. The request body is the image file itself."""
        data = await read_upload(request)
        return await run_in_threadpool(tags.set_image, conn, settings.tag_images_dir, tag_uid, data)

    @app.post("/api/tags/{tag_uid}/image-url")
    def tag_image_from_url(tag_uid: str, body: ImageUrl, conn: sqlite3.Connection = Depends(get_db)):
        """Set a tag's image from a picture on the web: the server downloads it."""
        tags.find_tag(conn, tag_uid)  # 404 before downloading anything
        return tags.set_image(conn, settings.tag_images_dir, tag_uid, download(body.url))

    @app.delete("/api/tags/{tag_uid}/image")
    def delete_tag_image(tag_uid: str, conn: sqlite3.Connection = Depends(get_db)):
        return tags.remove_image(conn, settings.tag_images_dir, tag_uid)

    @app.get("/api/tags/{tag_uid}/image")
    def get_tag_image(tag_uid: str, conn: sqlite3.Connection = Depends(get_db)):
        path = tags.tag_image_file(conn, settings.tag_images_dir, tag_uid)
        return FileResponse(path, media_type="image/jpeg", headers=THUMB_HEADERS)

    @app.get("/api/items/{item_uid}/thumb")
    def get_item_thumb(item_uid: str, conn: sqlite3.Connection = Depends(get_db)):
        return item_thumb(conn, item_uid)

    # ---- Uploaded images for videos and folders without one on the NAS ----

    @app.put("/api/items/{item_uid}/image")
    async def upload_video_image(item_uid: str, request: Request, conn: sqlite3.Connection = Depends(get_db)):
        data = await read_upload(request)
        return await run_in_threadpool(custom_images.set_video_image, conn, settings.images_dir, item_uid, data)

    @app.post("/api/items/{item_uid}/image-url")
    def video_image_from_url(item_uid: str, body: ImageUrl, conn: sqlite3.Connection = Depends(get_db)):
        browse.item_detail(conn, item_uid)  # 404 before downloading anything
        return custom_images.set_video_image(conn, settings.images_dir, item_uid, download(body.url))

    @app.delete("/api/items/{item_uid}/image")
    def delete_video_image(item_uid: str, conn: sqlite3.Connection = Depends(get_db)):
        return custom_images.remove_video_image(conn, settings.images_dir, item_uid)

    @app.put("/api/libraries/{library_uid}/folder-image")
    async def upload_folder_image(
        library_uid: str, request: Request, path: str = "", conn: sqlite3.Connection = Depends(get_db)
    ):
        library_id = library_pk(conn, library_uid)
        data = await read_upload(request)
        return await run_in_threadpool(
            custom_images.set_folder_image, conn, settings.images_dir, library_id, path, data
        )

    @app.post("/api/libraries/{library_uid}/folder-image-url")
    def folder_image_from_url(
        library_uid: str, body: ImageUrl, path: str = "", conn: sqlite3.Connection = Depends(get_db)
    ):
        library_id = library_pk(conn, library_uid)
        browse.browse(conn, library_id, path)  # 404 for an unknown folder before downloading
        return custom_images.set_folder_image(conn, settings.images_dir, library_id, path, download(body.url))

    @app.delete("/api/libraries/{library_uid}/folder-image")
    def delete_folder_image(library_uid: str, path: str = "", conn: sqlite3.Connection = Depends(get_db)):
        library_id = library_pk(conn, library_uid)
        return custom_images.remove_folder_image(conn, settings.images_dir, library_id, path)

    def media_file(conn: sqlite3.Connection, item_uid: str) -> tuple[sqlite3.Row, Path]:
        row = conn.execute("SELECT * FROM media_items WHERE uid = ?", (item_uid,)).fetchone()
        if row is None:
            raise HTTPException(404, "Video not found.")
        path = library_file(conn, row["library_id"], row["rel_path"])
        if not path.is_file():
            raise HTTPException(404, "The video file is missing. Is the NAS connected?")
        return row, path

    @app.api_route("/api/items/{item_uid}/file", methods=["GET", "HEAD"])
    def get_item_file(item_uid: str, conn: sqlite3.Connection = Depends(get_db)):
        """The original file, with range requests so the browser can seek."""
        row, path = media_file(conn, item_uid)
        return FileResponse(path, media_type=playback.direct_content_type(row["rel_path"]))

    def stream_source(item_uid: str) -> tuple[sqlite3.Row, Path]:
        """Look the video up on a short-lived connection (closed before streaming)."""
        conn = connect(settings.db_path)
        try:
            return media_file(conn, item_uid)
        finally:
            conn.close()

    @app.get("/api/items/{item_uid}/stream")
    async def get_item_stream(item_uid: str, start: float = 0):
        """A fragmented MP4 made by ffmpeg, starting `start` seconds in."""
        # The database and the NAS can be slow: keep them off the event loop.
        row, path = await run_in_threadpool(stream_source, item_uid)
        if row["play_mode"] == "unsupported":
            raise HTTPException(409, "This video can't be played.")
        if start < 0 or (row["duration"] and start >= row["duration"]):
            raise HTTPException(416, "Start time is outside the video.")
        cmd = playback.stream_command(
            path,
            "transcode" if row["play_mode"] == "transcode" else "remux",
            start=start,
            interlaced=bool(row["interlaced"]),
            height=row["height"],
            audio_codec=row["audio_codec"],
        )
        stream = streams.stream(cmd)
        # Wait for the first bytes, so a file ffmpeg can't read gets a clear
        # error instead of an empty 200 response.
        try:
            first = await anext(stream)
        except playback.StreamBusy as exc:
            raise HTTPException(503, str(exc), headers={"Retry-After": "5"})
        except playback.StreamFailed as exc:
            raise HTTPException(502, str(exc))
        except StopAsyncIteration:
            raise HTTPException(502, "ffmpeg couldn't play this video.")

        async def body():
            try:
                yield first
                async for chunk in stream:
                    yield chunk
            finally:
                await stream.aclose()

        return StreamingResponse(body(), media_type="video/mp4", headers={"Cache-Control": "no-store"})

    def current_user(conn: sqlite3.Connection = Depends(get_db)) -> dict:
        """Who's asking. Until there's login, always the built-in local user."""
        return users.local_user(conn)

    @app.get("/api/me")
    def me(user: dict = Depends(current_user)):
        return {"id": user["uid"], "name": user["name"]}

    @app.get("/api/health")
    def health(conn: sqlite3.Connection = Depends(get_db)):
        """For Docker's health check: the database answers and ffmpeg is present."""
        conn.execute("SELECT 1").fetchone()
        return {"ok": True, "ffmpeg": ffmpeg_version()}

    @app.get("/api/folders")
    def browse_folders(path: str | None = None):
        return libraries.list_subfolders(settings.media_root, path)

    @app.get("/api/libraries")
    def list_all(conn: sqlite3.Connection = Depends(get_db)):
        return [library_out(row) for row in libraries.list_libraries(conn)]

    @app.post("/api/libraries", status_code=201)
    def create(body: LibraryCreate, conn: sqlite3.Connection = Depends(get_db)):
        library_id = libraries.create_library(conn, settings.media_root, body.name, body.path)
        return one_library(conn, library_id)

    @app.patch("/api/libraries/{library_uid}")
    def rename(library_uid: str, body: LibraryRename, conn: sqlite3.Connection = Depends(get_db)):
        library_id = library_pk(conn, library_uid)
        libraries.rename_library(conn, library_id, body.name)
        return one_library(conn, library_id)

    @app.delete("/api/libraries/{library_uid}", status_code=204)
    def delete(library_uid: str, conn: sqlite3.Connection = Depends(get_db)):
        library_id = library_pk(conn, library_uid)
        if scans.is_busy(library_id):
            raise HTTPException(409, "Wait for the scan to finish before removing this library.")
        libraries.delete_library(conn, library_id)
        scans.forget(library_id)
        custom_images.prune(conn, settings.images_dir)
        return Response(status_code=204)

    @app.post("/api/libraries/scan", status_code=202)
    def scan_all(conn: sqlite3.Connection = Depends(get_db)):
        return {row["uid"]: scans.request(row["id"]) for row in libraries.list_libraries(conn)}

    @app.post("/api/libraries/{library_uid}/scan", status_code=202)
    def scan_one(library_uid: str, conn: sqlite3.Connection = Depends(get_db)):
        return scans.request(library_pk(conn, library_uid))

    app.mount("/", AppFiles(directory=STATIC_DIR, html=True), name="static")
    return app


def ffmpeg_version() -> str | None:
    """e.g. "n9.0.2-3-ga5923073bf-20260924", or None if ffmpeg isn't installed."""
    if not shutil.which("ffmpeg"):
        return None
    out = subprocess.run(["ffmpeg", "-hide_banner", "-version"], capture_output=True, text=True).stdout
    first = out.splitlines()[0] if out else ""
    return first.split()[2] if first.startswith("ffmpeg version") else first or None


def app() -> FastAPI:
    """Entry point for `uvicorn --factory reel.main:app`."""
    return create_app()
