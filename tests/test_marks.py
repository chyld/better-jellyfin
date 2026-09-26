"""Marks: spots in a video saved from the player, listed on its page."""
import os

import pytest

from conftest import make_files


def write(path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


@pytest.fixture
def video(client, media_root):
    make_files(media_root, "Tapes/a.mpg", "Tapes/b.mpg")      # the fake probe says 60 s long
    lib = client.post("/api/libraries", json={"name": "Tapes", "path": str(media_root / "Tapes")}).json()["id"]
    client.post(f"/api/libraries/{lib}/scan")
    client.scans.wait_idle()
    items = client.get(f"/api/libraries/{lib}/browse").json()["items"]
    return {"lib": lib, "id": next(i["id"] for i in items if i["title"] == "a")}


def mark(client, video_id, time):
    res = client.post(f"/api/items/{video_id}/marks", json={"time": time})
    assert res.status_code == 200, res.text
    return res.json()


def times(marks):
    return [m["time"] for m in marks]


def test_no_marks_to_begin_with(client, video):
    assert client.get(f"/api/items/{video['id']}/marks").json() == []
    assert client.get(f"/api/items/{video['id']}").json()["marks"] == []


def test_many_marks_listed_earliest_first(client, video):
    for t in (35.0, 5.5, 50.2, 12.0, 1.0, 20.0, 25.0, 30.0, 40.0, 45.0, 55.0):
        marks = mark(client, video["id"], t)
    assert times(marks) == [1.0, 5.5, 12.0, 20.0, 25.0, 30.0, 35.0, 40.0, 45.0, 50.2, 55.0]
    assert all(len(m["id"]) == 36 for m in marks)
    assert times(client.get(f"/api/items/{video['id']}").json()["marks"]) == times(marks)


def test_the_same_spot_isnt_marked_twice(client, video):
    mark(client, video["id"], 335.0 % 60)
    assert times(mark(client, video["id"], 35.4)) == [35.0]      # within a second: the same spot
    assert times(mark(client, video["id"], 36.5)) == [35.0, 36.5]


def test_marks_stay_inside_the_video(client, video):
    assert times(mark(client, video["id"], 3600)) == [60.0]       # past the end: the end
    for bad in ({"time": -1}, {"time": "soon"}, {}):
        assert client.post(f"/api/items/{video['id']}/marks", json=bad).status_code == 422
    assert client.post("/api/items/nope/marks", json={"time": 1}).status_code == 404


def test_deleting_a_mark(client, video):
    marks = mark(client, video["id"], 10)
    marks = mark(client, video["id"], 20)
    left = client.delete(f"/api/items/{video['id']}/marks/{marks[0]['id']}").json()
    assert times(left) == [20.0]
    assert client.delete(f"/api/items/{video['id']}/marks/{marks[0]['id']}").status_code == 404
    other = client.get(f"/api/libraries/{video['lib']}/browse").json()["items"]
    b = next(i["id"] for i in other if i["title"] == "b")
    assert client.delete(f"/api/items/{b}/marks/{left[0]['id']}").status_code == 404   # not b's


def test_marks_follow_a_moved_file(client, media_root, fake_probe):
    write(media_root / "Old/clip.mp4", b"distinct video " * 20000)
    lib = client.post("/api/libraries", json={"name": "M", "path": str(media_root)}).json()["id"]
    client.post(f"/api/libraries/{lib}/scan")
    client.scans.wait_idle()
    video_id = client.get(f"/api/libraries/{lib}/browse", params={"path": "Old"}).json()["items"][0]["id"]
    mark(client, video_id, 42)
    (media_root / "New").mkdir()
    os.rename(media_root / "Old/clip.mp4", media_root / "New/clip.mp4")
    client.post(f"/api/libraries/{lib}/scan")
    client.scans.wait_idle()
    moved = client.get(f"/api/libraries/{lib}/browse", params={"path": "New"}).json()["items"][0]["id"]
    assert moved == video_id and times(client.get(f"/api/items/{moved}/marks").json()) == [42.0]


def test_marks_go_with_the_video(client, video, settings):
    mark(client, video["id"], 10)
    import sqlite3
    conn = sqlite3.connect(settings.db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("DELETE FROM media_items WHERE uid = ?", (video["id"],))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM marks").fetchone()[0] == 0
    conn.close()
