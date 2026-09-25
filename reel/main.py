import asyncio
import contextlib
import functools
import logging
import shutil
import sqlite3
import subprocess
from collections.abc import Iterator
from contextlib import asynccontextmanager
from urllib.parse import urlencode
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import browse, catalog, fetch, hls, libraries, pictures, playback, tags, users
from .paths import OutsideRoot, resolve_inside
from .plan import HLS_SUPPORT, Capabilities, Plan, plan as make_plan
from .config import DataLock, Settings
from .db import connect, init_db
from .images import MAX_UPLOAD_BYTES, Thumbnailer, ThumbnailError, save_frame
from .scan_manager import ScanManager
from .thumbnails import THUMB_HEADERS, Thumbnails

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



class LibraryCreate(BaseModel):
    name: str
    path: str


class LibraryRename(BaseModel):
    name: str


class TagAdd(BaseModel):
    name: str


class TagRename(BaseModel):
    name: str


class Snapshot(BaseModel):
    time: float = Field(ge=0, allow_inf_nan=False)   # seconds into the video


class ImageUrl(BaseModel):
    url: str


def create_app(settings: Settings | None = None, scan_manager: ScanManager | None = None) -> FastAPI:
    """Build the app. Nothing here changes the data folder: that happens when the
    server starts (see startup()), once this process holds the data-folder lock."""
    settings = settings or Settings.from_env()
    settings.check_data_dir()
    streams = playback.StreamManager(playback.StreamLimits(max_streams=settings.max_streams))
    watching = playback.Watching(streams)   # background work gives way while someone watches
    scans = scan_manager or ScanManager(
        settings.db_path, workers=settings.probe_workers, missing_grace=settings.missing_grace,
        media_root=settings.media_root, busy=watching,
    )
    thumbs = Thumbnailer(settings.thumbs_dir)
    thumbnails = Thumbnails(settings.db_path, settings.media_root, settings.images_dir, thumbs, watching)
    hls_sessions = hls.HlsManager(settings.hls_dir, streams, cache_limit=settings.hls_cache_mb * 1024**2)
    # Pictures whose video, folder or tag is gone are cleaned up after every scan
    # (and at startup). Once the scan queue runs dry, thumbnails of old picture
    # versions are deleted and missing ones made, giving way to new scans, to
    # anyone watching, and to shutdown.
    scans.after_scan = lambda conn: pictures.prune(conn, settings.images_dir)

    def after_batch(library_ids: list[int], should_stop) -> bool:
        thumbnails.prune()
        return thumbnails.warm(library_ids, lambda: should_stop() or watching())

    scans.after_batch = after_batch
    tools: dict[str, str | None] = {}   # ffmpeg/ffprobe versions, checked at startup

    def startup() -> DataLock:
        """Everything that touches the data folder, in order, once it's ours."""
        lock = settings.lock_data_dir()
        try:
            init_db(settings.db_path)
            pictures.move_old_tag_images(settings.data_dir, settings.images_dir)
            conn = connect(settings.db_path)
            try:
                pictures.adopt_unversioned_files(conn, settings.images_dir)
                pictures.prune(conn, settings.images_dir)
            finally:
                conn.close()
            hls_sessions.start()
            tools.update(ffmpeg=tool_version("ffmpeg"), ffprobe=tool_version("ffprobe"))
            for name, version in tools.items():
                if version is None:
                    logging.getLogger("reel").error("%s isn't available: videos can't be scanned or converted", name)
        except BaseException:
            lock.release()
            raise
        return lock

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        lock = await run_in_threadpool(startup)
        scans.start()
        housekeeping = asyncio.create_task(hls_sessions.run_housekeeping())
        try:
            yield
        finally:
            housekeeping.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await housekeeping
            await hls_sessions.shutdown()
            await streams.shutdown()
            thumbs.stop()   # a thumbnail being made after a scan ends now too
            # A scan stops between files; wait for it off the event loop.
            await run_in_threadpool(scans.stop)
            lock.release()

    app = FastAPI(title="Reel", lifespan=lifespan)
    # Docker's health check polls /api/health; keep it out of the access log.
    logging.getLogger("uvicorn.access").addFilter(lambda record: "/api/health" not in record.getMessage())
    app.state.settings = settings
    app.state.scans = scans
    app.state.streams = streams
    app.state.hls = hls_sessions
    app.state.watching = watching

    def get_db() -> Iterator[sqlite3.Connection]:
        conn = connect(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    # Closed when the route function returns, before the response is sent: a
    # direct-play download can last hours and needs no database meanwhile.
    Db = Depends(get_db, scope="function")

    def library_out(row: dict) -> dict:
        # The API only ever shows the UUID; the integer id stays internal.
        out = {**row, "id": row["uid"], "scan": scans.status(row["id"])}
        del out["uid"]
        return out

    def one_library(conn: sqlite3.Connection, library_id: int) -> dict:
        row = libraries.library_summary(conn, library_id)
        if row is None:  # removed in the meantime
            raise catalog.NotFound("Library not found.")
        return library_out(row)

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

    @app.exception_handler(ThumbnailError)
    async def bad_image(request: Request, exc: ThumbnailError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(catalog.NotFound)
    async def not_found(request: Request, exc: catalog.NotFound):
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
            raise catalog.NotFound("Library not found.")
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

    @app.get("/api/libraries/{library_uid}/browse")
    def browse_folder(library_uid: str, path: str = "", sort: str = "name", limit: int | None = None,
                      offset: int = 0, conn: sqlite3.Connection = Db):
        """A folder's subfolders, and its videos one page at a time (`limit`, `offset`)."""
        return browse.browse(conn, library_pk(conn, library_uid), path, sort, limit=limit, offset=offset)

    @app.get("/api/libraries/{library_uid}/folder-art")
    async def get_folder_art(library_uid: str, path: str = "", v: str | None = None):
        """`v` is the picture's version (see the listing): it makes the answer cacheable for good."""
        return await thumbnails.response(await run_in_threadpool(thumbnails.folder_picture, library_uid, path), v)

    @app.get("/api/items/{item_uid}")
    def get_item(item_uid: str, conn: sqlite3.Connection = Db):
        item = browse.item_detail(conn, item_uid)
        return {**item, "tags": tags.item_tags(conn, tags.item_pk(conn, item_uid))}

    @app.post("/api/items/{item_uid}/tags")
    def add_item_tag(item_uid: str, body: TagAdd, conn: sqlite3.Connection = Db):
        """Tag a video; returns the video's tags."""
        return tags.add_tag(conn, item_uid, body.name)

    @app.delete("/api/items/{item_uid}/tags/{tag_uid}")
    def remove_item_tag(item_uid: str, tag_uid: str, conn: sqlite3.Connection = Db):
        """Untag a video; returns the video's remaining tags."""
        return tags.remove_tag(conn, item_uid, tag_uid)

    @app.get("/api/tags")
    def list_tags(conn: sqlite3.Connection = Db):
        return tags.list_tags(conn)

    @app.get("/api/tags/{tag_uid}")
    def get_tag(tag_uid: str, limit: int | None = None, offset: int = 0, conn: sqlite3.Connection = Db):
        return tags.tag_videos(conn, tag_uid, limit=limit, offset=offset)

    @app.patch("/api/tags/{tag_uid}")
    def rename_tag(tag_uid: str, body: TagRename, conn: sqlite3.Connection = Db):
        """Rename a tag; renaming it to an existing tag merges the two."""
        return tags.rename_tag(conn, tag_uid, body.name, settings.images_dir)

    @app.delete("/api/tags/{tag_uid}", status_code=204)
    def delete_tag(tag_uid: str, conn: sqlite3.Connection = Db):
        """Delete a tag from every video."""
        tags.delete_tag(conn, tag_uid, settings.images_dir)
        return Response(status_code=204)

    @app.get("/api/tags/{tag_uid}/image")
    def get_tag_image(tag_uid: str, conn: sqlite3.Connection = Db):
        path = pictures.picture_file(conn, settings.images_dir, pictures.TagPicture(tag_uid))
        if path is None:
            raise HTTPException(404, "This tag has no image.")
        return FileResponse(path, media_type="image/jpeg", headers=THUMB_HEADERS)

    @app.get("/api/items/{item_uid}/thumb")
    async def get_item_thumb(item_uid: str, v: str | None = None):
        """`v` is the picture's version (custom_image or poster_rev): as above."""
        return await thumbnails.response(await run_in_threadpool(thumbnails.item_picture, item_uid), v)

    # ---- Your pictures for tags, videos and folders (they win over NAS pictures) ----

    def picture_routes(path: str, owner_of, answer) -> None:
        """Upload (PUT, the body is the image), from a URL (POST <path>-url) and
        remove (DELETE) a picture. `owner_of` finds whose it is from the request;
        `answer(conn, owner, version)` is the response."""

        @app.put(path)
        async def upload_picture(request: Request, owner=Depends(owner_of), conn: sqlite3.Connection = Db):
            data = await read_upload(request)
            version = await run_in_threadpool(pictures.set_uploaded, conn, settings.images_dir, owner, data)
            return answer(conn, owner, version)

        @app.post(path + "-url")
        def picture_from_url(body: ImageUrl, owner=Depends(owner_of), conn: sqlite3.Connection = Db):
            owner.check(conn)  # 404 before downloading anything
            version = pictures.set_uploaded(conn, settings.images_dir, owner, download(body.url))
            return answer(conn, owner, version)

        @app.delete(path)
        def remove_picture(owner=Depends(owner_of), conn: sqlite3.Connection = Db):
            pictures.remove_picture(conn, settings.images_dir, owner)
            return answer(conn, owner, None)

    def folder_owner(library_uid: str, path: str = "", conn: sqlite3.Connection = Db) -> pictures.FolderPicture:
        return pictures.FolderPicture(library_pk(conn, library_uid), path)

    picture_routes("/api/tags/{tag_uid}/image", lambda tag_uid: pictures.TagPicture(tag_uid),
                   lambda conn, owner, version: tags.tag_summary(conn, owner.uid))
    picture_routes("/api/items/{item_uid}/image", lambda item_uid: pictures.VideoPicture(item_uid),
                   lambda conn, owner, version: {"custom_image": version})
    picture_routes("/api/libraries/{library_uid}/folder-image", folder_owner,
                   lambda conn, owner, version: {"custom_art": version})

    @app.post("/api/items/{item_uid}/snapshot")
    def video_image_from_frame(item_uid: str, body: Snapshot, conn: sqlite3.Connection = Db):
        """Use the frame at `time` seconds as the video's picture, replacing any."""
        row, path = media_file(conn, item_uid)
        # The very last moment may have no frame to decode: stay a little before it.
        at = min(body.time, max(0.0, row["duration"] - 0.5)) if row["duration"] else body.time
        interlaced = bool(row["interlaced"])
        try:
            version = pictures.set_picture(conn, settings.images_dir, pictures.VideoPicture(item_uid),
                                           lambda out: save_frame(path, at, out, interlaced=interlaced))
        except ThumbnailError as exc:
            raise HTTPException(502, f"Couldn't take a picture from the video ({exc}).")
        return {"custom_image": version}

    def media_file(conn: sqlite3.Connection, item_uid: str) -> tuple[sqlite3.Row, Path]:
        row = conn.execute("SELECT * FROM media_items WHERE uid = ?", (item_uid,)).fetchone()
        if row is None:
            raise HTTPException(404, "Video not found.")
        path = library_file(conn, row["library_id"], row["rel_path"])
        if not path.is_file():
            raise HTTPException(404, "The video file is missing. Is the NAS connected?")
        return row, path

    @app.get("/api/items/{item_uid}/file", operation_id="get_item_file")
    @app.head("/api/items/{item_uid}/file", operation_id="head_item_file")
    def get_item_file(item_uid: str, conn: sqlite3.Connection = Db):
        """The original file, with range requests so the browser can seek."""
        row, path = media_file(conn, item_uid)
        watching.saw_playback()
        return FileResponse(path, media_type=playback.direct_content_type(row["rel_path"]))

    def stream_source(item_uid: str) -> tuple[sqlite3.Row, Path]:
        """Look the video up on a short-lived connection (closed before streaming)."""
        conn = connect(settings.db_path)
        try:
            return media_file(conn, item_uid)
        finally:
            conn.close()

    @app.get("/api/items/{item_uid}/plan")
    def get_item_plan(item_uid: str, video: str | None = None, audio: str | None = None,
                      hls_support: str = "none", conn: sqlite3.Connection = Db):
        """How this browser should play the video.

        `video`/`audio` list the codecs it can decode (e.g. video=h264,hevc&audio=aac,ac3);
        `hls_support` is native (Safari), mse (hls.js can run) or none. Returns the
        mode, the delivery (file | progressive | hls) and the URL to load.
        """
        row = conn.execute("SELECT * FROM media_items WHERE uid = ?", (item_uid,)).fetchone()
        if row is None:
            raise HTTPException(404, "Video not found.")
        if hls_support not in HLS_SUPPORT:
            hls_support = "none"
        caps = Capabilities.from_query(video, audio)
        p = make_plan(row, caps, hls_support)
        query = urlencode({"video": ",".join(sorted(caps.video)), "audio": ",".join(sorted(caps.audio))})
        url = {
            "file": f"/api/items/{item_uid}/file",
            "progressive": f"/api/items/{item_uid}/stream?{query}",
            "hls": f"/api/items/{item_uid}/hls.m3u8?{query}&hls_support={hls_support}",
        }.get(p.delivery)
        return {"mode": p.mode, "video": p.video, "audio": p.audio, "streamed": p.streamed,
                "delivery": p.delivery, "note": p.note, "url": url}

    # ---- HLS ----

    async def hls_session_for(item_uid: str, caps: Capabilities, hls_support: str) -> tuple[str, float]:
        """Open (or find) the HLS session for this video and browser: (its id, the length)."""
        row, path = await run_in_threadpool(stream_source, item_uid)
        p = make_plan(row, caps, hls_support)
        if p.mode == "unsupported":
            raise HTTPException(409, "This video can't be played.")
        if not row["duration"]:
            raise HTTPException(409, "This video's length is unknown, so it can't be split into segments.")
        if p.delivery != "hls":
            raise HTTPException(409, f"This video is played as {p.delivery}, not HLS.")
        try:
            rev = await run_in_threadpool(hls.revision, path)
        except OSError:
            raise HTTPException(404, "The video file is missing. Is the NAS connected?")
        source = hls.Source(path, p, row["duration"], bool(row["interlaced"]), row["height"],
                            row["audio_codec"], rev, width=row["width"])
        return await hls_sessions.open(item_uid, source), row["duration"]

    def hls_query(video: str | None, audio: str | None, hls_support: str) -> str:
        caps = Capabilities.from_query(video, audio)
        return urlencode({"video": ",".join(sorted(caps.video)), "audio": ",".join(sorted(caps.audio)),
                          "hls_support": hls_support if hls_support in HLS_SUPPORT else "mse"})

    @app.get("/api/items/{item_uid}/hls.m3u8")
    async def get_hls_playlist(item_uid: str, video: str | None = None, audio: str | None = None,
                               hls_support: str = "mse"):
        """The whole video as an HLS playlist of 6-second segments (video converted).
        Each load is a new viewer, with its own position and encoder."""
        sid, duration = await hls_session_for(item_uid, Capabilities.from_query(video, audio), hls_support)
        text = hls.playlist(duration, sid, hls.new_viewer(), hls_query(video, audio, hls_support))
        return Response(text, media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store"})

    @app.get("/api/items/{item_uid}/hls/{sid}/{viewer}/{number}.ts")
    async def get_hls_segment(item_uid: str, sid: str, viewer: str, number: int, video: str | None = None,
                              audio: str | None = None, hls_support: str = "mse"):
        if not hls.valid_viewer(viewer):
            raise HTTPException(404, "Unknown player.")
        session = hls_sessions.get(sid)
        if session is None or session.item_uid != item_uid:
            # Expired (a long pause) or from before a restart: make it again from
            # the URL. If that gives another session, the file has changed.
            fresh, _ = await hls_session_for(item_uid, Capabilities.from_query(video, audio), hls_support)
            if fresh != sid:
                raise HTTPException(410, "The video has changed. Reload the player.")
            session = hls_sessions.get(sid)
        watching.saw_playback()
        try:
            path = await hls_sessions.media_segment(session, viewer, number)
        except playback.StreamBusy as exc:
            raise HTTPException(503, str(exc), headers={"Retry-After": "5"})
        except hls.HlsGone as exc:
            raise HTTPException(410, str(exc))
        except hls.HlsError as exc:
            raise HTTPException(502, str(exc))
        return FileResponse(path, media_type="video/mp2t", headers={"Cache-Control": "no-cache"})

    @app.get("/api/items/{item_uid}/stream")
    async def get_item_stream(item_uid: str, start: float = 0, video: str | None = None, audio: str | None = None):
        """A fragmented MP4 made by ffmpeg, starting `start` seconds in. `video` and
        `audio` are the browser's codecs (as for /plan); each track is copied if
        the browser can play it, and converted otherwise."""
        # The database and the NAS can be slow: keep them off the event loop.
        watching.saw_playback()
        row, path = await run_in_threadpool(stream_source, item_uid)
        p = make_plan(row, Capabilities.from_query(video, audio))
        if p.mode == "unsupported":
            raise HTTPException(409, "This video can't be played.")
        if not p.streamed:
            # The browser could play the file itself; stream it as a straight copy anyway.
            p = Plan("remux", video="copy", audio=None if row["audio_codec"] is None else "copy",
                     delivery="progressive")
        if start < 0 or (row["duration"] and start >= row["duration"]):
            raise HTTPException(416, "Start time is outside the video.")
        cmd = playback.stream_command(
            path,
            p,
            start=start,
            interlaced=bool(row["interlaced"]),
            height=row["height"],
            width=row["width"],
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

    def current_user(conn: sqlite3.Connection = Db) -> dict:
        """Who's asking. Until there's login, always the built-in local user."""
        return users.local_user(conn)

    @app.get("/api/me")
    def me(user: dict = Depends(current_user)):
        return {"id": user["uid"], "name": user["name"]}

    @app.get("/api/health")
    def health(conn: sqlite3.Connection = Db):
        """For Docker's health check (503 when not ready): the database answers,
        ffmpeg and ffprobe were found at startup, and the scanner is running. Also
        how busy the server is."""
        conn.execute("SELECT 1").fetchone()
        ready = bool(tools.get("ffmpeg") and tools.get("ffprobe")) and scans.alive()
        body = {
            "ok": ready,
            "ffmpeg": tools.get("ffmpeg"),
            "ffprobe": tools.get("ffprobe"),
            "scanner": {"alive": scans.alive(), "queued": scans.queued()},
            "streams": {"active": len(streams.active), "limit": settings.max_streams},
            "hls": hls_sessions.status(),
        }
        return JSONResponse(body, status_code=200 if ready else 503)

    @app.get("/api/folders")
    def browse_folders(path: str | None = None):
        return libraries.list_subfolders(settings.media_root, path)

    @app.get("/api/libraries")
    def list_all(conn: sqlite3.Connection = Db):
        return [library_out(row) for row in libraries.list_libraries(conn)]

    @app.post("/api/libraries", status_code=201)
    def create(body: LibraryCreate, conn: sqlite3.Connection = Db):
        library_id = libraries.create_library(conn, settings.media_root, body.name, body.path)
        return one_library(conn, library_id)

    @app.patch("/api/libraries/{library_uid}")
    def rename(library_uid: str, body: LibraryRename, conn: sqlite3.Connection = Db):
        library_id = library_pk(conn, library_uid)
        libraries.rename_library(conn, library_id, body.name)
        return one_library(conn, library_id)

    @app.delete("/api/libraries/{library_uid}", status_code=204)
    def delete(library_uid: str, conn: sqlite3.Connection = Db):
        library_id = library_pk(conn, library_uid)
        if scans.is_busy(library_id):
            raise HTTPException(409, "Wait for the scan to finish before removing this library.")
        libraries.delete_library(conn, library_id)
        scans.forget(library_id)
        pictures.prune(conn, settings.images_dir)
        return Response(status_code=204)

    @app.post("/api/libraries/scan", status_code=202)
    def scan_all(conn: sqlite3.Connection = Db):
        return {row["uid"]: scans.request(row["id"]) for row in libraries.list_libraries(conn)}

    @app.post("/api/libraries/{library_uid}/scan", status_code=202)
    def scan_one(library_uid: str, conn: sqlite3.Connection = Db):
        return scans.request(library_pk(conn, library_uid))

    app.mount("/", AppFiles(directory=STATIC_DIR, html=True), name="static")
    return app


@functools.cache  # a process checks each tool once
def tool_version(tool: str) -> str | None:
    """ffmpeg's or ffprobe's version, e.g. "n9.0.2-3-ga5923073bf-20260924"; None if
    it isn't installed or doesn't answer within 10 seconds."""
    if not shutil.which(tool):
        return None
    try:
        out = subprocess.run([tool, "-hide_banner", "-version"], capture_output=True, text=True, timeout=10).stdout
    except (subprocess.TimeoutExpired, OSError):
        return None
    first = out.splitlines()[0] if out else ""
    return first.split()[2] if first.startswith(f"{tool} version") else first or None


def app() -> FastAPI:
    """Entry point for `uvicorn --factory reel.main:app`."""
    return create_app()
