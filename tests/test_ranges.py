"""Ranges: stretches of a video marked on its edit page (just marked, skipped when
playing, or a clip). The video file itself is never touched."""
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


def add(client, video_id, **body):
    return client.post(f"/api/items/{video_id}/ranges", json=body)


def spans(ranges):
    return [(r["start"], r["end"], r["kind"], r["label"]) for r in ranges]


def test_no_ranges_to_begin_with(client, video):
    assert client.get(f"/api/items/{video['id']}/ranges").json() == []
    assert client.get(f"/api/items/{video['id']}").json()["ranges"] == []


def test_ranges_of_each_kind_listed_earliest_first(client, video):
    add(client, video["id"], start=30, end=40, kind="clip", label="  The   cake ")
    add(client, video["id"], start=5.04, end=9.96, kind="skip")
    res = add(client, video["id"], start=12, end=20)
    assert res.status_code == 200, res.text
    assert spans(res.json()) == [(5.0, 10.0, "skip", ""), (12.0, 20.0, "range", ""), (30.0, 40.0, "clip", "The cake")]
    assert spans(client.get(f"/api/items/{video['id']}").json()["ranges"]) == spans(res.json())


@pytest.mark.parametrize("body, message", [
    ({"start": 10, "end": 10.2}, "at least 0.5 seconds"),
    ({"start": 20, "end": 10}, "at least 0.5 seconds"),
    ({"start": 61, "end": 70}, "past the end"),
    ({"start": 1, "end": 5, "kind": "delete"}, "one of"),
    ({"start": 1, "end": 5, "label": "x" * 101}, "at most 100"),
])
def test_ranges_that_make_no_sense_are_refused(client, video, body, message):
    res = add(client, video["id"], **body)
    assert res.status_code == 400 and message in res.json()["detail"]
    assert client.get(f"/api/items/{video['id']}/ranges").json() == []


def test_a_range_past_the_end_stops_at_the_end(client, video):
    assert spans(add(client, video["id"], start=50, end=500).json()) == [(50.0, 60.0, "range", "")]


def test_changing_and_deleting_a_range(client, video):
    [r] = add(client, video["id"], start=1, end=5).json()
    res = client.put(f"/api/items/{video['id']}/ranges/{r['id']}", json={"start": 2, "end": 8, "kind": "clip", "label": "Hi"})
    assert res.status_code == 200 and spans(res.json()) == [(2.0, 8.0, "clip", "Hi")] and res.json()[0]["id"] == r["id"]
    # Another video's range can't be changed or deleted through this one.
    assert client.put(f"/api/items/{video['other']}/ranges/{r['id']}", json={"start": 1, "end": 2}).status_code == 404
    assert client.delete(f"/api/items/{video['other']}/ranges/{r['id']}").status_code == 404
    assert client.delete(f"/api/items/{video['id']}/ranges/{r['id']}").json() == []
    assert client.delete(f"/api/items/{video['id']}/ranges/{r['id']}").status_code == 404


def test_ranges_of_a_missing_video(client):
    assert client.get("/api/items/00000000-0000-0000-0000-000000000000/ranges").status_code == 404
    assert add(client, "00000000-0000-0000-0000-000000000000", start=1, end=5).status_code == 404


def test_ranges_follow_a_moved_file_and_go_with_the_video(client, media_root, video, settings):
    import sqlite3
    add(client, video["id"], start=1, end=5, kind="clip")
    (media_root / "Tapes/moved").mkdir()
    (media_root / "Tapes/a.mpg").rename(media_root / "Tapes/moved/a.mpg")
    client.post(f"/api/libraries/{video['lib']}/scan")
    client.scans.wait_idle()
    assert spans(client.get(f"/api/items/{video['id']}/ranges").json()) == [(1.0, 5.0, "clip", "")]
    client.delete(f"/api/libraries/{video['lib']}")
    conn = sqlite3.connect(settings.db_path)
    assert conn.execute("SELECT COUNT(*) FROM ranges").fetchone()[0] == 0
    conn.close()
