"""Thumbnails made with real ffmpeg: posters, folder art and video frames."""
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
    (folder / "posters/clip.mp4").symlink_to(clips / "h264_aac.mp4")
    (folder / "posters/tape.mpg").symlink_to(clips / "vhs_interlaced.mpg")
    image(folder / "posters/clip.png")
    image(folder / "posters/folder.png", size="1024x1536", color="blue")
    (folder / "posters/tape.png").write_bytes(b"not really a png")
    (folder / "frames/old.avi").symlink_to(clips / "xvid_mp3.avi")
    (folder / "frames/broken.mp4").symlink_to(clips / "corrupt.mp4")
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
    cached = list(settings.thumbs_dir.rglob("*.jpg"))
    assert len(cached) == 1
    mtime = cached[0].stat().st_mtime
    assert client.get(url).content == first
    assert cached[0].stat().st_mtime == mtime


def test_replaced_poster_gets_a_new_thumbnail(client, lib, media_root, settings):
    url = f"/api/items/{item_id(client, lib, 'posters', 'clip')}/thumb"
    first = client.get(url).content
    image(media_root / "Media/posters/clip.png", size="640x480", color="green")
    second = client.get(url).content
    assert first != second
    assert len(list(settings.thumbs_dir.rglob("*.jpg"))) == 2


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
    (folder / "a.mp4").symlink_to(clips / "h264_aac.mp4")
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
