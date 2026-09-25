"""Taking a video's picture from one of its frames (the player's "Use as preview")."""
import subprocess
from functools import partial

import pytest
from fastapi.testclient import TestClient

from reel.db import init_db
from reel.images import frame_at
from reel.main import create_app
from reel.probe import probe
from reel.scan_manager import ScanManager
from reel.scanner import scan_library

from conftest import requires_ffmpeg

pytestmark = requires_ffmpeg


def make_clip(path, *extra_output_args, interlaced=False):
    """10 seconds: red for the first 5, blue after (so a frame shows when it's from)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=320x240:r=25:d=10",
         "-f", "lavfi", "-i", "sine=duration=10",
         "-vf", "drawbox=x=0:y=0:w=iw:h=ih:color=blue:t=fill:enable='gte(t,5)'" + (",setfield=tff" if interlaced else ""),
         "-shortest",
         *extra_output_args, str(path)],
        check=True,
    )
    return path


def colour(image: bytes, tmp_path) -> str:
    """"red" or "blue": the image's average colour, whichever is stronger."""
    src = tmp_path / "check.img"
    src.write_bytes(image)
    rgb = subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-vf", "scale=1:1", "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "pipe:1"], capture_output=True, check=True).stdout
    return "red" if rgb[0] > rgb[2] else "blue"


@pytest.fixture
def client(settings):
    init_db(settings.db_path)
    manager = ScanManager(settings.db_path, scan_fn=partial(scan_library, probe_fn=probe))
    with TestClient(create_app(settings, manager)) as c:
        c.scans = manager
        yield c


@pytest.fixture
def videos(client, media_root):
    make_clip(media_root / "Clips/plain.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac")
    make_clip(media_root / "Clips/tape.mpg", "-c:v", "mpeg2video", "-flags", "+ilme+ildct", "-c:a", "mp2",
              interlaced=True)
    # An image on the NAS for plain.mp4: green.
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=green:s=320x240",
                    "-frames:v", "1", str(media_root / "Clips/plain.png")], check=True)
    lib = client.post("/api/libraries", json={"name": "Clips", "path": str(media_root / "Clips")}).json()["id"]
    client.post(f"/api/libraries/{lib}/scan")
    client.scans.wait_idle()
    return {i["title"]: i for i in client.get(f"/api/libraries/{lib}/browse").json()["items"]}


def snapshot(client, video, time):
    return client.post(f"/api/items/{video['id']}/snapshot", json={"time": time})


def thumb(client, video):
    res = client.get(f"/api/items/{video['id']}/thumb")
    assert res.status_code == 200
    return res.content


def test_frame_at_takes_the_frame_at_that_time(tmp_path):
    clip = make_clip(tmp_path / "c.mp4", "-c:v", "libx264", "-pix_fmt", "yuv420p")
    assert colour(frame_at(clip, 2.0), tmp_path) == "red"
    assert colour(frame_at(clip, 7.5), tmp_path) == "blue"
    assert frame_at(clip, 0).startswith(b"\x89PNG")


def test_snapshot_becomes_the_preview_and_replaces_an_earlier_one(client, videos, settings, tmp_path):
    video = videos["plain"]
    first = snapshot(client, video, 2.0)
    assert first.status_code == 200
    assert colour(thumb(client, video), tmp_path) == "red"
    second = snapshot(client, video, 7.0).json()["custom_image"]
    assert second != first.json()["custom_image"]
    assert colour(thumb(client, video), tmp_path) == "blue"
    stored = list((settings.images_dir / "videos").glob("*.jpg"))
    assert [p.name for p in stored] == [f"{video['id']}-{second}.jpg"]    # the old one is gone


def test_snapshot_wins_over_the_nas_image_until_removed(client, videos, tmp_path):
    video = videos["plain"]
    assert video["has_poster"]
    nas = thumb(client, video)
    snapshot(client, video, 7.0)
    assert colour(thumb(client, video), tmp_path) == "blue"
    client.delete(f"/api/items/{video['id']}/image")
    assert thumb(client, video) == nas                                    # the NAS image again


def test_snapshot_of_interlaced_video(client, videos, tmp_path):
    video = videos["tape"]
    assert client.get(f"/api/items/{video['id']}").json()["interlaced"]
    assert snapshot(client, video, 6.0).status_code == 200
    assert colour(thumb(client, video), tmp_path) == "blue"


def test_time_past_the_end_takes_the_last_moment(client, videos, tmp_path):
    video = videos["plain"]
    assert snapshot(client, video, 3600).status_code == 200
    assert colour(thumb(client, video), tmp_path) == "blue"


@pytest.mark.parametrize("body", [{"time": -1}, {"time": "soon"}, {}])
def test_bad_times_are_refused(client, videos, body):
    assert client.post(f"/api/items/{videos['plain']['id']}/snapshot", json=body).status_code == 422


def test_missing_video_and_missing_file(client, videos, media_root):
    assert client.post("/api/items/nope/snapshot", json={"time": 1}).status_code == 404
    (media_root / "Clips/plain.mp4").unlink()
    res = snapshot(client, videos["plain"], 1)
    assert res.status_code == 404 and "NAS" in res.json()["detail"]


def test_unreadable_video_gives_a_clear_error(client, videos, media_root):
    (media_root / "Clips/plain.mp4").write_bytes(b"not a video any more")
    res = snapshot(client, videos["plain"], 1)
    assert res.status_code == 502 and res.json()["detail"].startswith("Couldn't take a picture")
