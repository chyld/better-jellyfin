"""Thumbnails made with real ffmpeg: posters, folder art and video frames."""
import shutil
import subprocess
from functools import partial

import pytest
from fastapi.testclient import TestClient

from reel.db import init_db
from reel.images import Thumbnailer, ThumbnailError
from reel.main import create_app
from reel.probe import probe
from reel.scan_manager import ScanManager
from reel.scanner import scan_library

from conftest import requires_ffmpeg

pytestmark = [requires_ffmpeg, pytest.mark.ffmpeg]


def image(path, size="1920x1080", color="red"):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={color}:s={size}", "-frames:v", "1", str(path)],
        check=True,
    )


def jpeg_size(data: bytes, tmp_path):
    f = tmp_path / "check.jpg"
    f.write_bytes(data)
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(f)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return tuple(int(x) for x in out.split(","))


@pytest.fixture
def client(settings):
    """A client whose scans run the real ffprobe."""
    init_db(settings.db_path)
    manager = ScanManager(settings.db_path, scan_fn=partial(scan_library, probe_fn=probe))
    with TestClient(create_app(settings, manager)) as c:
        c.scans = manager
        yield c


@pytest.fixture
def lib(client, media_root, clips):
    folder = media_root / "Media"
    (folder / "posters").mkdir(parents=True)
    (folder / "frames").mkdir()
    shutil.copyfile(clips / "h264_aac.mp4", folder / "posters/clip.mp4")
    shutil.copyfile(clips / "vhs_interlaced.mpg", folder / "posters/tape.mpg")
    image(folder / "posters/clip.png")
    image(folder / "posters/folder.png", size="1024x1536", color="blue")
    (folder / "posters/tape.png").write_bytes(b"not really a png")
    shutil.copyfile(clips / "xvid_mp3.avi", folder / "frames/old.avi")
    shutil.copyfile(clips / "corrupt.mp4", folder / "frames/broken.mp4")
    lib = client.post("/api/libraries", json={"name": "Media", "path": str(folder)}).json()
    client.post(f"/api/libraries/{lib['id']}/scan")
    client.scans.wait_idle()
    return lib["id"]


def item_id(client, lib, folder, title):
    page = client.get(f"/api/libraries/{lib}/browse", params={"path": folder}).json()
    return next(i["id"] for i in page["items"] if i["title"] == title)


def test_poster_is_shrunk_to_a_jpeg(client, lib, tmp_path):
    res = client.get(f"/api/items/{item_id(client, lib, 'posters', 'clip')}/thumb")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"
    assert "max-age" in res.headers["cache-control"]
    # Videos are shown landscape: a 1920x1080 image becomes 640x360.
    assert jpeg_size(res.content, tmp_path) == (640, 360)


def test_thumbnails_are_cached(client, lib, settings):
    url = f"/api/items/{item_id(client, lib, 'posters', 'clip')}/thumb"
    first = client.get(url).content
    cached = {p: p.stat().st_mtime for p in settings.thumbs_dir.rglob("*.jpg")}
    assert cached
    assert client.get(url).content == first
    assert {p: p.stat().st_mtime for p in settings.thumbs_dir.rglob("*.jpg")} == cached   # nothing remade


def rescan(client, lib):
    client.post(f"/api/libraries/{lib}/scan")
    client.scans.wait_idle()


def poster(client, lib, title):
    page = client.get(f"/api/libraries/{lib}/browse", params={"path": "posters"}).json()
    return next(i for i in page["items"] if i["title"] == title)


def test_replaced_poster_gets_a_new_thumbnail_after_the_next_scan(client, lib, media_root, settings):
    """A thumbnail is found by the poster's recorded version, without asking the NAS;
    a replaced poster has a new version once a scan has seen it."""
    video = poster(client, lib, "clip")
    url = f"/api/items/{video['id']}/thumb"
    first = client.get(url, params={"v": video["poster_rev"]}).content
    image(media_root / "Media/posters/clip.png", size="640x480", color="green")
    assert client.get(url).content == first                 # not scanned yet: the thumbnail we have
    rescan(client, lib)
    newer = poster(client, lib, "clip")
    assert newer["poster_rev"] != video["poster_rev"]
    assert client.get(url, params={"v": newer["poster_rev"]}).content != first


def test_a_cached_thumbnail_is_served_without_the_nas(client, lib, media_root, monkeypatch):
    video = poster(client, lib, "clip")
    url = f"/api/items/{video['id']}/thumb"
    made = client.get(url).content
    from reel import main
    monkeypatch.setattr(main, "resolve_inside", lambda *a, **k: (_ for _ in ()).throw(OSError("NAS gone")))
    assert client.get(url).content == made                  # no path check, no stat: just the cache


def test_versioned_thumbnail_urls_can_be_cached_for_good(client, lib):
    video = poster(client, lib, "clip")
    url = f"/api/items/{video['id']}/thumb"
    assert "immutable" in client.get(url, params={"v": video["poster_rev"]}).headers["cache-control"]
    assert "immutable" not in client.get(url).headers["cache-control"]
    assert "immutable" not in client.get(url, params={"v": "old"}).headers["cache-control"]


def test_video_without_preview_has_no_image(client, lib):
    """No zombie.png beside zombie.mp4: the browser shows the video placeholder."""
    assert client.get(f"/api/items/{item_id(client, lib, 'frames', 'old')}/thumb").status_code == 404


def test_broken_preview_has_no_image(client, lib):
    # tape.png is not really a PNG; the browser falls back to the placeholder.
    assert client.get(f"/api/items/{item_id(client, lib, 'posters', 'tape')}/thumb").status_code == 404


def test_unplayable_video_without_poster_has_no_image(client, lib):
    assert client.get(f"/api/items/{item_id(client, lib, 'frames', 'broken')}/thumb").status_code == 404


def test_folder_art(client, lib, tmp_path):
    res = client.get(f"/api/libraries/{lib}/folder-art", params={"path": "posters"})
    assert res.status_code == 200
    assert jpeg_size(res.content, tmp_path) == (480, 720)
    assert client.get(f"/api/libraries/{lib}/folder-art", params={"path": "frames"}).status_code == 404


def test_library_root_folder_preview(client, media_root, clips, tmp_path):
    folder = media_root / "Rooted"
    folder.mkdir()
    shutil.copyfile(clips / "h264_aac.mp4", folder / "a.mp4")
    image(folder / "folder.png", size="800x800")
    lib = client.post("/api/libraries", json={"name": "Rooted", "path": str(folder)}).json()
    client.post(f"/api/libraries/{lib['id']}/scan")
    client.scans.wait_idle()
    res = client.get(f"/api/libraries/{lib['id']}/folder-art", params={"path": ""})
    assert res.status_code == 200
    assert jpeg_size(res.content, tmp_path) == (480, 720)  # 800x800 cropped to 2:3


def test_thumbnailer_errors(tmp_path):
    thumbs = Thumbnailer(tmp_path / "cache")
    with pytest.raises(ThumbnailError):
        thumbs.from_image(tmp_path / "missing.png")
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"nope")
    with pytest.raises(ThumbnailError):
        thumbs.from_image(bad)
    # Failed attempts leave nothing behind in the cache.
    assert [p for p in (tmp_path / "cache").rglob("*") if p.is_file()] == []


@pytest.mark.parametrize(
    "size, poster",
    [
        ("1920x1080", (480, 720)),   # landscape: centre strip
        ("640x480", (320, 480)),     # small 4:3 VHS frame: cropped, never enlarged
        ("1024x1536", (480, 720)),   # already 2:3: just shrunk
        ("600x1800", (480, 720)),    # very tall: centre band
        ("200x300", (200, 300)),     # tiny: left as is
    ],
)
def test_previews_are_cropped_to_posters(tmp_path, size, poster):
    src = tmp_path / "src.png"
    image(src, size=size)
    out = Thumbnailer(tmp_path / "cache").from_image(src)
    assert jpeg_size(out.read_bytes(), tmp_path) == poster


@pytest.mark.parametrize(
    "size, landscape",
    [
        ("1920x1080", (640, 360)),   # already 16:9: just shrunk
        ("640x480", (640, 360)),     # 4:3 VHS frame: top and bottom trimmed
        ("1024x1536", (640, 360)),   # portrait: centre band
        ("320x240", (320, 180)),     # small: cropped, never enlarged
    ],
)
def test_video_previews_are_cropped_to_landscape(tmp_path, size, landscape):
    src = tmp_path / "src.png"
    image(src, size=size)
    out = Thumbnailer(tmp_path / "cache").from_image(src, "landscape")
    assert jpeg_size(out.read_bytes(), tmp_path) == landscape


def test_poster_and_landscape_are_cached_separately(tmp_path):
    src = tmp_path / "src.png"
    image(src)
    thumbs = Thumbnailer(tmp_path / "cache")
    assert thumbs.from_image(src, "poster") != thumbs.from_image(src, "landscape")


def test_a_scan_makes_missing_thumbnails_and_drops_old_versions(client, lib, media_root, settings):
    video = poster(client, lib, "clip")
    made = settings.thumbs_dir.rglob("*.jpg")
    assert list(made)                                         # made after the scan, before any view
    thumbs = client.app.state.settings.thumbs_dir
    old = {p for p in thumbs.rglob("*.jpg")}
    image(media_root / "Media/posters/clip.png", size="640x480", color="green")
    import os, time
    for p in old:                                             # old enough to be cleaned up
        os.utime(p, (time.time() - 7200, time.time() - 7200))
    rescan(client, lib)
    now = {p for p in thumbs.rglob("*.jpg")}
    assert now - old                                          # the new version was made...
    gone = old - now
    assert len(gone) == 1                                     # ...and only the replaced one's old thumbnail went
    assert poster(client, lib, "clip")["poster_rev"] != video["poster_rev"]


def test_no_thumbnails_are_made_after_a_scan_while_something_plays(client, lib, settings, monkeypatch):
    import shutil
    shutil.rmtree(settings.thumbs_dir, ignore_errors=True)
    client.app.state.streams.active.add(object())            # a video is playing
    try:
        rescan(client, lib)
    finally:
        client.app.state.streams.active.clear()
    assert not list(settings.thumbs_dir.rglob("*.jpg"))
