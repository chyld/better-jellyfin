"""Browsing big folders: indexed folder lookups and paged video lists."""
import os

import pytest

from reel import db
from reel.browse import browse
from reel.catalog import MAX_PAGE_SIZE
from reel.db import connect, init_db
from reel.libraries import create_library
from reel.scanner import scan_library
from reel.sorting import natural_key, parent_dir, sort_key

from conftest import make_files


def test_sort_key_orders_numbers_by_value():
    names = ["clip10", "Clip2", "clip1", "clip02b", "alpha", "007", "7a", "clip1x", "Clip 3"]
    assert sorted(names, key=lambda n: sort_key(n, n)) == [
        "007", "7a", "alpha", "Clip 3", "clip1", "clip1x", "Clip2", "clip02b", "clip10",
    ]


def test_huge_numbers_still_sort():
    names = ["x" + "9" * 30, "x2"]
    assert sorted(names, key=lambda n: sort_key(n, n))[0] == "x2"


def test_sort_key_breaks_ties_by_path():
    assert sort_key("a", "x/a.mp4") < sort_key("a", "y/a.mp4")


def test_parent_dir():
    assert parent_dir("a.mp4") == ""
    assert parent_dir("A/B/c.mp4") == "A/B"


# ---- the API -------------------------------------------------------------------------


@pytest.fixture
def big(client, media_root):
    make_files(media_root, *[f"Many/clip{n}.mp4" for n in range(1, 26)], "Many/Sub/deep.mp4")
    lib = client.post("/api/libraries", json={"name": "Media", "path": str(media_root)}).json()["id"]
    client.post(f"/api/libraries/{lib}/scan")
    client.scans.wait_idle()
    return lib


def get(client, lib, **params):
    res = client.get(f"/api/libraries/{lib}/browse", params={"path": "Many", **params})
    assert res.status_code == 200, res.text
    return res.json()


def test_pages_cover_every_video_once_in_order(client, big):
    seen = []
    offset = 0
    while True:
        page = get(client, big, limit=10, offset=offset)
        assert page["total_items"] == 25 and page["limit"] == 10 and page["offset"] == offset
        if not page["items"]:
            break
        seen += [i["title"] for i in page["items"]]
        offset += len(page["items"])
    assert seen == [f"clip{n}" for n in range(1, 26)]


def test_every_page_lists_the_subfolders(client, big):
    page = get(client, big, limit=5, offset=20)
    assert [f["name"] for f in page["folders"]] == ["Sub"]
    assert len(page["items"]) == 5


def test_default_and_out_of_range_page_sizes(client, big):
    assert len(get(client, big)["items"]) == 25
    assert get(client, big, limit=0)["limit"] == 1
    assert get(client, big, limit=10_000)["limit"] == MAX_PAGE_SIZE
    assert get(client, big, offset=-5)["offset"] == 0
    assert get(client, big, offset=100)["items"] == []


def test_tag_pages(client, big):
    items = get(client, big)["items"]
    for item in items[:7]:
        tag = client.post(f"/api/items/{item['id']}/tags", json={"name": "family"}).json()[0]
    first = client.get(f"/api/tags/{tag['id']}", params={"limit": 5}).json()
    rest = client.get(f"/api/tags/{tag['id']}", params={"limit": 5, "offset": 5}).json()
    assert first["total_items"] == rest["total_items"] == 7
    assert [i["title"] for i in first["items"] + rest["items"]] == [i["title"] for i in items[:7]]


# ---- folders ---------------------------------------------------------------------------


def test_similar_folder_names_are_not_mixed_up(conn, media_root, fake_probe):
    """"Show 1" must not pick up "Show 1 extra", "Show 1.5", "Show 10" or "Show 1-x"."""
    make_files(media_root, "Show 1/S1/a.mp4", "Show 1 extra/b.mp4", "Show 1.5/c.mp4",
               "Show 10/d.mp4", "Show 1-x/e.mp4", "Show 1/f.mp4")
    lib = create_library(conn, media_root, "Media", str(media_root))
    scan_library(conn, lib, probe_fn=fake_probe)
    page = browse(conn, lib, "Show 1")
    assert [(f["name"], f["item_count"]) for f in page["folders"]] == [("S1", 1)]
    assert [i["title"] for i in page["items"]] == ["f"]
    root = browse(conn, lib, "")
    assert [f["name"] for f in root["folders"]] == ["Show 1", "Show 1 extra", "Show 1-x", "Show 1.5", "Show 10"]
    assert [f["item_count"] for f in root["folders"]] == [2, 1, 1, 1, 1]


def test_folder_with_only_subfolders_is_found(conn, media_root, fake_probe):
    make_files(media_root, "Outer/Inner/a.mp4")
    lib = create_library(conn, media_root, "Media", str(media_root))
    scan_library(conn, lib, probe_fn=fake_probe)
    page = browse(conn, lib, "Outer")
    assert page["total_items"] == 0 and [f["name"] for f in page["folders"]] == ["Inner"]


# ---- keeping the index columns right ------------------------------------------------------


def columns(conn, rel_path):
    r = conn.execute("SELECT parent_dir, title_key, title FROM media_items WHERE rel_path = ?",
                     (rel_path,)).fetchone()
    return dict(r) if r else None


def test_scan_fills_folder_and_order(conn, media_root, fake_probe):
    make_files(media_root, "A/B/Clip 7.mp4", "top.mp4")
    lib = create_library(conn, media_root, "Media", str(media_root))
    scan_library(conn, lib, probe_fn=fake_probe)
    row = columns(conn, "A/B/Clip 7.mp4")
    assert row["parent_dir"] == "A/B" and row["title_key"] == sort_key(row["title"], "A/B/Clip 7.mp4")
    assert columns(conn, "top.mp4")["parent_dir"] == ""


def test_moved_video_follows_its_folder(conn, media_root, fake_probe):
    path = media_root / "Old/clip.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"distinct video " * 20000)
    lib = create_library(conn, media_root, "Media", str(media_root))
    scan_library(conn, lib, probe_fn=fake_probe)
    (media_root / "New").mkdir()
    os.rename(path, media_root / "New/renamed.mp4")
    assert scan_library(conn, lib, probe_fn=fake_probe)["moved"] == 1
    row = columns(conn, "New/renamed.mp4")
    assert row["parent_dir"] == "New" and row["title_key"] == sort_key(row["title"], "New/renamed.mp4")
    assert [i["id"] for i in browse(conn, lib, "New")["items"]] and browse(conn, lib, "")["folders"][0]["name"] == "New"


def test_upgrade_fills_folder_and_order(tmp_path, monkeypatch):
    path = tmp_path / "reel.db"
    monkeypatch.setattr(db, "MIGRATIONS", [m for m in db.MIGRATIONS if m[0] < 6])
    init_db(path)
    conn = connect(path)
    conn.execute("INSERT INTO libraries (uid, name, path) VALUES ('l', 'L', '/m')")
    for rel in ("Show/clip10.mp4", "Show/clip2.mp4", "loose.mp4"):
        title = rel.rsplit("/", 1)[-1][:-4]
        conn.execute("INSERT INTO media_items (uid, library_id, rel_path, title, size, mtime) "
                     "VALUES (?, 1, ?, ?, 1, 1)", (rel, rel, title))
    conn.commit()
    conn.close()
    monkeypatch.undo()
    init_db(path)
    conn = connect(path)
    assert columns(conn, "Show/clip2.mp4")["parent_dir"] == "Show"
    assert columns(conn, "loose.mp4")["parent_dir"] == ""
    page = browse(conn, 1, "Show")
    assert [i["title"] for i in page["items"]] == ["clip2", "clip10"]
    assert [f["name"] for f in browse(conn, 1, "")["folders"]] == ["Show"]


def test_browsing_uses_the_index(conn):
    def plan(sql, *args):
        return " ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, args))

    here = "library_id = ? AND parent_dir = ? AND missing_since IS NULL"
    assert "media_items_browse" in plan(f"SELECT * FROM media_items WHERE {here} ORDER BY title_key LIMIT 5", 1, "A")
    assert "TEMP B-TREE" not in plan(f"SELECT * FROM media_items WHERE {here} ORDER BY title_key LIMIT 5", 1, "A")
    below = plan("SELECT parent_dir, COUNT(*) FROM media_items WHERE library_id = ? AND parent_dir >= ? "
                 "AND parent_dir < ? AND missing_since IS NULL GROUP BY parent_dir", 1, "A/", "A0")
    assert "media_items_browse" in below and "TEMP B-TREE" not in below
