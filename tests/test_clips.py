"""Clips: stretches of a video made on its edit page from two marks. The video
file itself is never touched."""
import sqlite3

import pytest

from conftest import make_files


@pytest.fixture
def video(client, media_root):
    make_files(media_root, "Tapes/a.mpg", "Tapes/b.mpg")      # the fake probe says 60 s long
    lib = client.post("/api/libraries", json={"name": "Tapes", "path": str(media_root / "Tapes")}).json()["id"]
    client.post(f"/api/libraries/{lib}/scan")
    client.scans.wait_idle()
    items = client.get(f"/api/libraries/{lib}/browse").json()["items"]
    return {"lib": lib, "id": next(i["id"] for i in items if i["title"] == "a"),
            "other": next(i["id"] for i in items if i["title"] == "b")}


def make(client, video_id, start, end):
    return client.post(f"/api/items/{video_id}/clips", json={"start": start, "end": end})


def names(clips):
    return [(c["name"], c["start"], c["end"]) for c in clips]


def test_no_clips_to_begin_with(client, video):
    assert client.get(f"/api/items/{video['id']}/clips").json() == []
    assert client.get(f"/api/items/{video['id']}").json()["clips"] == []


def test_clips_are_numbered_in_the_order_they_were_made(client, video):
    make(client, video["id"], 30, 40)
    make(client, video["id"], 9.96, 5.04)                      # marks in either order
    res = make(client, video["id"], 50, 500)                   # past the end: stops at the end
    assert res.status_code == 200, res.text
    assert names(res.json()) == [("Clip 1", 30.0, 40.0), ("Clip 2", 5.0, 10.0), ("Clip 3", 50.0, 60.0)]
    assert res.json() == client.get(f"/api/items/{video['id']}").json()["clips"]
    assert names(make(client, video["other"], 1, 2).json()) == [("Clip 1", 1.0, 2.0)]   # each video counts its own


def test_numbers_go_on_after_a_deleted_clip(client, video):
    first, second = (make(client, video["id"], t, t + 5).json()[-1] for t in (1, 10))
    left = client.delete(f"/api/items/{video['id']}/clips/{first['id']}").json()
    assert names(left) == [("Clip 2", 10.0, 15.0)]
    assert names(make(client, video["id"], 20, 25).json()) == [("Clip 2", 10.0, 15.0), ("Clip 3", 20.0, 25.0)]


@pytest.mark.parametrize("start, end, message", [
    (10, 10.2, "at least 0.5 seconds apart"),
    (61, 70, "after the end"),
])
def test_clips_that_make_no_sense_are_refused(client, video, start, end, message):
    res = make(client, video["id"], start, end)
    assert res.status_code == 400 and message in res.json()["detail"]
    assert client.get(f"/api/items/{video['id']}/clips").json() == []


def test_deleting_a_clip(client, video):
    [clip] = make(client, video["id"], 1, 5).json()
    assert client.delete(f"/api/items/{video['other']}/clips/{clip['id']}").status_code == 404   # not that video's
    assert client.delete(f"/api/items/{video['id']}/clips/{clip['id']}").json() == []
    assert client.delete(f"/api/items/{video['id']}/clips/{clip['id']}").status_code == 404


def test_clips_of_a_missing_video(client):
    assert client.get("/api/items/00000000-0000-0000-0000-000000000000/clips").status_code == 404
    assert make(client, "00000000-0000-0000-0000-000000000000", 1, 5).status_code == 404


def test_clips_follow_a_moved_file_and_go_with_the_video(client, media_root, video, settings):
    make(client, video["id"], 1, 5)
    (media_root / "Tapes/moved").mkdir()
    (media_root / "Tapes/a.mpg").rename(media_root / "Tapes/moved/a.mpg")
    client.post(f"/api/libraries/{video['lib']}/scan")
    client.scans.wait_idle()
    assert names(client.get(f"/api/items/{video['id']}/clips").json()) == [("Clip 1", 1.0, 5.0)]
    client.delete(f"/api/libraries/{video['lib']}")
    conn = sqlite3.connect(settings.db_path)
    assert conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0] == 0
    conn.close()
