"""MP4 copies: a video in the wrong container (MPEG-TS named .mp4), copied once
into a real MP4 in the data folder and played from there instead."""
import dataclasses
import os
import subprocess
import time
from functools import partial

import pytest
from fastapi.testclient import TestClient

from reel import copies
from reel.db import connect, init_db
from reel.main import create_app
from reel.probe import probe
from reel.scan_manager import ScanManager
from reel.scanner import scan_library

from conftest import requires_ffmpeg, settle_pictures

pytestmark = requires_ffmpeg


def start(settings):
    init_db(settings.db_path)
    manager = ScanManager(settings.db_path, scan_fn=partial(scan_library, probe_fn=probe))
    client = TestClient(create_app(settings, manager))
    client.scans = manager
    return client


@pytest.fixture
def client(settings):
    with start(settings) as c:
        yield c


def scan(client, lib):
    client.post(f"/api/libraries/{lib}/scan")
    client.scans.wait_idle()


@pytest.fixture
def videos(client, media_root, clips):
    folder = media_root / "Films"
    folder.mkdir()
    for name in ("transport_stream.mp4", "h264_aac.mp4", "xvid_mp3.avi", "h264_aac.mkv"):
        (folder / name).write_bytes((clips / name).read_bytes())
    lib = client.post("/api/libraries", json={"name": "Films", "path": str(folder)}).json()["id"]
    scan(client, lib)
    items = client.get(f"/api/libraries/{lib}/browse").json()["items"]
    return {"lib": lib, "folder": folder, **{i["rel_path"]: i["id"] for i in items}}


def wait_for_copy(client, video, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        item = client.get(f"/api/items/{video}").json()
        if not item["copy_job"] or item["copy_job"]["state"] == "error":
            return item
        time.sleep(0.1)
    raise AssertionError("the copy never finished")


def video_packets(path):
    return subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=size",
                           "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True).stdout.split()


def test_a_transport_stream_is_copied_into_a_real_mp4_and_played_from_it(client, videos, settings, tmp_path):
    video = videos["transport_stream.mp4"]
    before = client.get(f"/api/items/{video}").json()
    assert (before["can_copy"], before["copy_size"], before["copy_job"]) == (True, None, None)
    assert client.get(f"/api/items/{video}/plan").json()["mode"] == "remux"

    res = client.post(f"/api/items/{video}/mp4-copy")
    assert res.status_code == 202 and res.json()["state"] in ("queued", "running")
    item = wait_for_copy(client, video)
    assert item["copy_job"] is None and item["copy_size"] > 0

    # Now it's a real MP4, played directly, with the same video packets.
    plan = client.get(f"/api/items/{video}/plan").json()
    assert (plan["mode"], plan["delivery"], plan["url"]) == ("direct", "file", f"/api/items/{video}/file")
    played = client.get(plan["url"])
    assert played.status_code == 200 and played.headers["content-type"] == "video/mp4"
    assert len(played.content) == item["copy_size"]
    (tmp_path / "played.mp4").write_bytes(played.content)
    assert probe(tmp_path / "played.mp4").container.startswith("mov,mp4")
    # Copied, not re-encoded: every video packet is there (TS and MP4 frame H.264 a
    # little differently, so their sizes differ by a few bytes).
    assert len(video_packets(tmp_path / "played.mp4")) == len(video_packets(videos["folder"] / "transport_stream.mp4"))
    assert client.get(plan["url"], headers={"Range": "bytes=0-99"}).status_code == 206

    card = next(i for i in client.get(f"/api/libraries/{videos['lib']}/browse").json()["items"] if i["id"] == video)
    assert (card["type"], card["has_copy"], card["play_mode"]) == ("TS", True, "direct")   # its own format, plus COPY
    assert [p.name for p in settings.copies_dir.iterdir()] == [f"{video}-{copy_version(settings, video)}.mp4"]
    assert client.get("/api/health").json()["copies"]["count"] == 1
    assert (videos["folder"] / "transport_stream.mp4").read_bytes()[:1] == b"G"        # the NAS file: untouched


def copy_version(settings, video):
    conn = connect(settings.db_path)
    try:
        return conn.execute("SELECT mp4_copy FROM media_items WHERE uid = ?", (video,)).fetchone()[0]
    finally:
        conn.close()


def test_removing_the_copy_plays_the_original_again(client, videos, settings):
    video = videos["transport_stream.mp4"]
    client.post(f"/api/items/{video}/mp4-copy")
    wait_for_copy(client, video)
    assert client.delete(f"/api/items/{video}/mp4-copy").status_code == 204
    item = client.get(f"/api/items/{video}").json()
    assert item["copy_size"] is None and item["can_copy"] is True
    assert client.get(f"/api/items/{video}/plan").json()["mode"] == "remux"
    settle_copies(settings)
    assert list(settings.copies_dir.glob("*.mp4")) == []


def settle_copies(settings):
    """Let copy files age past the clean-up's grace period, then run it."""
    old = time.time() - 7200
    for path in settings.copies_dir.glob("*.mp4"):
        os.utime(path, (old, old))
    conn = connect(settings.db_path)
    try:
        copies.prune(conn, settings.copies_dir)
    finally:
        conn.close()


def test_a_changed_file_on_the_nas_makes_the_copy_stale(client, videos, settings, clips):
    video = videos["transport_stream.mp4"]
    client.post(f"/api/items/{video}/mp4-copy")
    wait_for_copy(client, video)
    path = videos["folder"] / "transport_stream.mp4"
    path.write_bytes((clips / "transport_stream.mp4").read_bytes())
    os.utime(path, (1_000_000_000, 1_000_000_000))                  # a new version of the file
    scan(client, videos["lib"])                                    # the scan notices; the clean-up runs
    item = client.get(f"/api/items/{video}").json()
    assert item["copy_size"] is None
    assert client.get(f"/api/items/{video}/plan").json()["mode"] == "remux"
    settle_copies(settings)
    assert list(settings.copies_dir.glob("*.mp4")) == []


def test_a_moved_file_keeps_its_copy(client, videos):
    video = videos["transport_stream.mp4"]
    client.post(f"/api/items/{video}/mp4-copy")
    wait_for_copy(client, video)
    (videos["folder"] / "Moved").mkdir()
    os.rename(videos["folder"] / "transport_stream.mp4", videos["folder"] / "Moved/transport_stream.mp4")
    scan(client, videos["lib"])
    assert client.get(f"/api/items/{video}").json()["copy_size"]
    assert client.get(f"/api/items/{video}/plan").json()["mode"] == "direct"


@pytest.mark.parametrize("name", ["h264_aac.mp4", "xvid_mp3.avi"])
def test_only_videos_a_copy_would_make_direct_can_have_one(client, videos, name):
    """Already an MP4, or needs converting anyway: no copy is offered."""
    assert client.get(f"/api/items/{videos[name]}").json()["can_copy"] is False
    assert client.post(f"/api/items/{videos[name]}/mp4-copy").status_code == 409


def test_an_mkv_can_be_copied_too(client, videos):
    video = videos["h264_aac.mkv"]
    assert client.get(f"/api/items/{video}").json()["can_copy"] is True
    client.post(f"/api/items/{video}/mp4-copy")
    assert wait_for_copy(client, video)["copy_size"]
    assert client.get(f"/api/items/{video}/plan").json()["delivery"] == "file"


def test_asking_twice_makes_one_copy_and_a_second_is_refused(client, videos, settings):
    video = videos["transport_stream.mp4"]
    client.post(f"/api/items/{video}/mp4-copy")
    client.post(f"/api/items/{video}/mp4-copy")
    wait_for_copy(client, video)
    assert len(list(settings.copies_dir.glob("*.mp4"))) == 1
    assert client.post(f"/api/items/{video}/mp4-copy").status_code == 409


def test_unknown_video(client):
    assert client.post("/api/items/nope/mp4-copy").status_code == 404
    assert client.delete("/api/items/nope/mp4-copy").status_code == 404


def test_no_copy_without_room_on_the_disk(settings, media_root, clips):
    (media_root / "Films").mkdir()
    (media_root / "Films/ts.mp4").write_bytes((clips / "transport_stream.mp4").read_bytes())
    tight = dataclasses.replace(settings, min_free_mb=10**9)
    with start(tight) as c:
        lib = c.post("/api/libraries", json={"name": "Films", "path": str(media_root / "Films")}).json()["id"]
        scan(c, lib)
        video = c.get(f"/api/libraries/{lib}/browse").json()["items"][0]["id"]
        res = c.post(f"/api/items/{video}/mp4-copy")
        assert res.status_code == 507 and "free disk space" in res.json()["detail"]


def test_a_copy_that_fails_says_why_and_leaves_nothing(client, videos, settings):
    video = videos["transport_stream.mp4"]
    (videos["folder"] / "transport_stream.mp4").write_bytes(b"not a video any more")   # (not rescanned)
    client.post(f"/api/items/{video}/mp4-copy")
    item = wait_for_copy(client, video)
    assert item["copy_job"]["state"] == "error" and "ffmpeg couldn't copy" in item["copy_job"]["error"]
    assert item["copy_size"] is None and list(settings.copies_dir.iterdir()) == []


def test_leftover_partial_copies_are_removed_at_startup(settings):
    settings.copies_dir.mkdir(parents=True)
    (settings.copies_dir / "x-1.part").write_bytes(b"half")
    with start(settings):
        pass
    assert list(settings.copies_dir.iterdir()) == []


def test_a_copy_goes_with_its_library(client, videos, settings):
    video = videos["transport_stream.mp4"]
    client.post(f"/api/items/{video}/mp4-copy")
    wait_for_copy(client, video)
    assert client.delete(f"/api/libraries/{videos['lib']}").status_code == 204
    assert list(settings.copies_dir.glob("*.mp4"))                  # retired: kept for the grace period
    settle_copies(settings)
    assert list(settings.copies_dir.glob("*.mp4")) == []
    settle_pictures(settings)


def test_the_copy_command_copies_both_tracks_with_the_index_first(tmp_path):
    cmd = copies.command(tmp_path / "a.mp4", tmp_path / "a.part", "aac")
    assert cmd[cmd.index("-c") + 1] == "copy" and "aac_adtstoasc" in cmd and "+faststart" in cmd
    assert cmd[cmd.index("-map") + 1] == "0:V:0"
    assert "aac_adtstoasc" not in copies.command(tmp_path / "a.mkv", tmp_path / "a.part", "mp3")


# ---- The Copies page ------------------------------------------------------------------------


def test_the_copies_page_lists_copies_and_failures(client, videos):
    assert client.get("/api/copies").json() == {"jobs": [], "copies": [], "totals": {"count": 0, "mb": 0}}
    good, bad = videos["transport_stream.mp4"], videos["h264_aac.mkv"]
    client.post(f"/api/items/{good}/mp4-copy")
    wait_for_copy(client, good)
    (videos["folder"] / "h264_aac.mkv").write_bytes(b"not a video any more")
    client.post(f"/api/items/{bad}/mp4-copy")
    wait_for_copy(client, bad)

    page = client.get("/api/copies").json()
    [failed] = page["jobs"]
    assert (failed["id"], failed["state"], failed["title"], failed["library_name"]) == (bad, "error", "h264_aac", "Films")
    assert "ffmpeg couldn't copy" in failed["error"]
    [made] = page["copies"]
    assert (made["id"], made["rel_path"], made["current"]) == (good, "transport_stream.mp4", True)
    assert made["copy_size"] > 0 and made["made_at"] and made["file_size"] > 0
    assert page["totals"]["count"] == 1

    client.delete(f"/api/items/{bad}/mp4-copy")                  # Dismiss
    assert client.get("/api/copies").json()["jobs"] == []


def test_jobs_are_listed_running_then_waiting_in_order_then_failed(tmp_path):
    manager = copies.CopyManager(tmp_path / "db", tmp_path / "copies")
    for uid, state in (("a", "error"), ("b", "queued"), ("c", "running"), ("d", "queued")):
        job = copies.Job(uid, tmp_path / uid, 1.0, None, "r")
        job.status["state"] = state
        manager.jobs[uid] = job
    assert [uid for uid, _ in manager.all()] == ["c", "b", "d", "a"]


def test_waiting_copies_say_their_place_in_line(conn, media_root, fake_probe, tmp_path):
    from reel.libraries import create_library

    from conftest import make_files
    make_files(media_root, "T/a.mkv", "T/b.mkv", "T/c.mkv")
    lib = create_library(conn, media_root, "T", str(media_root / "T"))
    scan_library(conn, lib, probe_fn=fake_probe)
    uid = {r["rel_path"]: r["uid"] for r in conn.execute("SELECT uid, rel_path FROM media_items")}
    jobs = [(uid["a.mkv"], {"state": "running", "progress": 0.5, "error": None}),
            (uid["b.mkv"], {"state": "queued", "progress": 0.0, "error": None}),
            (uid["c.mkv"], {"state": "queued", "progress": 0.0, "error": None}),
            ("gone", {"state": "queued", "progress": 0.0, "error": None})]        # its video was removed
    listed = copies.listing(conn, jobs)["jobs"]
    assert [(j["rel_path"], j.get("place")) for j in listed] == [("a.mkv", None), ("b.mkv", 1), ("c.mkv", 2)]


def test_copies_made_before_their_time_was_recorded_get_their_files_time(client, videos, settings):
    video = videos["transport_stream.mp4"]
    client.post(f"/api/items/{video}/mp4-copy")
    wait_for_copy(client, video)
    conn = connect(settings.db_path)
    conn.execute("UPDATE media_items SET mp4_copy_at = NULL")
    conn.commit()
    copies.adopt_times(conn, settings.copies_dir)
    assert conn.execute("SELECT mp4_copy_at FROM media_items WHERE uid = ?", (video,)).fetchone()[0]
    conn.close()
