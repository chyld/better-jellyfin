"""Browsing big folders: indexed folder lookups and paged video lists."""
import os

import pytest

from reel import db
from reel.browse import browse
from reel.catalog import MAX_PAGE_SIZE
from reel.db import connect, init_db
from reel.libraries import create_library
from reel.scanner import scan_library
from reel.sorting import folder_range, natural_key, parent_dir, path_key, sort_key

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


def test_path_key_keeps_each_folder_together_in_natural_order():
    paths = ["A b/y.mp4", "A/x.mp4", "A/S2/clip10.mp4", "A/S2/clip2.mp4", "a.mp4", "B/1.mp4", "A/clip9.mp4"]
    assert sorted(paths, key=path_key) == [
        "A/clip9.mp4", "A/S2/clip2.mp4", "A/S2/clip10.mp4", "A/x.mp4", "A b/y.mp4", "a.mp4", "B/1.mp4",
    ]


def test_look_alike_folders_stay_apart():
    """Names equal once case and padding are folded still sort as separate folders."""
    paths = ["Show 7/a.mp4", "Show 07/b.mp4", "Show 7/c.mp4", "Show 07/d.mp4", "beach/1.mp4", "Beach/2.mp4",
             "beach/3.mp4"]
    assert sorted(paths, key=path_key) == [
        "Beach/2.mp4", "beach/1.mp4", "beach/3.mp4", "Show 07/b.mp4", "Show 07/d.mp4", "Show 7/a.mp4", "Show 7/c.mp4",
    ]


@pytest.mark.parametrize("folder", ["Show 7", "Show 07", "ΑΣ", "ΟΔΟΣ/Σ", "İstanbul", "ß", "ﬁle", "x" + "9" * 30, "a b"])
def test_folder_range_holds_exactly_the_videos_below(folder):
    low, high = folder_range(folder)
    inside = [f"{folder}/v.mp4", f"{folder}/Σ/ς.mkv", f"{folder}/7/007.avi"]
    outside = [f"{folder}.mp4", f"{folder} x/v.mp4", f"{folder}0/v.mp4", f"{folder.lower()}X/v.mp4", "Show 7 b/v.mp4",
               ("show 7" if folder == "Show 7" else "Show 7") + "/v.mp4"]
    assert all(low <= path_key(p) < high for p in inside)
    assert not any(low <= path_key(p) < high for p in outside)


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


def test_show_all_pages_every_video_below_by_path(client, big):
    seen, offset = [], 0
    while True:
        page = get(client, big, all="true", limit=10, offset=offset)
        assert page["all"] is True and page["folders"] == [] and page["total_items"] == 26
        if not page["items"]:
            break
        seen += [i["rel_path"] for i in page["items"]]
        offset += len(page["items"])
    # "Many/Sub/deep.mp4" comes after clip1..clip25: "sub" > "clip" once case is folded.
    assert seen == [f"Many/clip{n}.mp4" for n in range(1, 26)] + ["Many/Sub/deep.mp4"]


def test_show_all_puts_each_videos_clips_right_after_it(client, big):
    """Clips follow their video, in the order made. Pages count videos only, so a
    video's clips are on its page, even when it ends the page; next_offset says
    where the next page starts."""
    videos = {i["rel_path"]: i["id"] for i in get(client, big, all="true")["items"]}
    for path, marks in [("Many/clip10.mp4", [(20, 25), (1, 5)]), ("Many/clip3.mp4", [(3, 4)]),
                        ("Many/Sub/deep.mp4", [(7, 9)])]:
        for start, end in marks:
            client.post(f"/api/items/{videos[path]}/clips", json={"start": start, "end": end})
    seen, offset = [], 0
    while True:
        page = get(client, big, all="true", limit=10, offset=offset)
        assert page["total_items"] == 26 and page["total_clips"] == 4
        if not page["items"]:
            break
        seen += [(i.get("kind", "video"), i["rel_path"], i.get("name")) for i in page["items"]]
        assert page["next_offset"] == offset + len([i for i in page["items"] if "kind" not in i])
        offset = page["next_offset"]
    order = [f"Many/clip{n}.mp4" for n in range(1, 26)] + ["Many/Sub/deep.mp4"]
    expected = []
    for path in order:
        expected.append(("video", path, None))
        expected += [("clip", path, name) for name in {"Many/clip10.mp4": ["Clip 1", "Clip 2"],
                                                      "Many/clip3.mp4": ["Clip 1"],
                                                      "Many/Sub/deep.mp4": ["Clip 1"]}.get(path, [])]
    assert seen == expected
    # clip10 is the 10th video: last on the first page of 10, with both its clips.
    first = get(client, big, all="true", limit=10)["items"]
    assert [i.get("name") for i in first[-3:]] == [None, "Clip 1", "Clip 2"]
    clip = first[-2]
    assert clip["video_id"] == videos["Many/clip10.mp4"] and (clip["start"], clip["end"]) == (20.0, 25.0)
    assert clip["title"] == "clip10"
    # Only below the folder shown.
    assert get(client, big, path="Many/Sub", all="true")["total_clips"] == 1


@pytest.mark.parametrize("sort", ["name", "year"])
def test_a_folder_puts_each_videos_clips_right_after_it(client, big, sort):
    """The plain folder view too, in either sort, paged by videos like Show all."""
    videos = {i["rel_path"]: i["id"] for i in get(client, big)["items"]}
    for start in (20, 1):
        client.post(f"/api/items/{videos['Many/clip10.mp4']}/clips", json={"start": start, "end": start + 4})
    whole = get(client, big, sort=sort, limit=500)
    assert whole["total_items"] == 25 and whole["total_clips"] == 2      # Sub's video isn't in this folder
    names = [(i["rel_path"], i.get("name")) for i in whole["items"]]
    at = names.index(("Many/clip10.mp4", None))
    assert names[at + 1:at + 3] == [("Many/clip10.mp4", "Clip 1"), ("Many/clip10.mp4", "Clip 2")]
    seen, offset = [], 0
    while True:
        page = get(client, big, sort=sort, limit=4, offset=offset)
        if not page["items"]:
            break
        seen += [(i["rel_path"], i.get("name")) for i in page["items"]]
        offset = page["next_offset"]
    assert seen == names
    assert get(client, big, path="Many/Sub")["total_clips"] == 0

def test_show_all_is_always_by_path(client, big):
    """There's no other order for it: sort=year is ignored."""
    by_year = get(client, big, all="true", sort="year")
    assert "sort" not in by_year
    assert [i["rel_path"] for i in by_year["items"]] == [i["rel_path"] for i in get(client, big, all="true")["items"]]
    assert get(client, big)["all"] is False


def test_show_all_at_the_library_top_and_missing_folders(client, big):
    top = client.get(f"/api/libraries/{big}/browse", params={"all": "true"}).json()
    assert top["total_items"] == 26 and top["breadcrumbs"] == [{"name": "Media", "path": ""}]
    assert client.get(f"/api/libraries/{big}/browse", params={"path": "Nope", "all": "true"}).status_code == 404


def test_show_all_keeps_similar_folders_apart(conn, media_root, fake_probe):
    """Keys fold case and number padding: "Show 7" must still not list "Show 07"'s videos."""
    make_files(media_root, "Show 7/a.mp4", "Show 7/S2/clip10.mp4", "Show 7/S2/clip2.mp4", "Show 07/b.mp4",
               "Show 7 b/c.mp4", "Show 70/d.mp4", "Show 7.mp4")
    lib = create_library(conn, media_root, "Media", str(media_root))
    scan_library(conn, lib, probe_fn=fake_probe)
    page = browse(conn, lib, "Show 7", show_all=True)
    assert [i["rel_path"] for i in page["items"]] == ["Show 7/a.mp4", "Show 7/S2/clip2.mp4", "Show 7/S2/clip10.mp4"]
    assert browse(conn, lib, "", show_all=True)["total_items"] == 7


def test_show_all_of_the_parent_keeps_look_alike_folders_apart(conn, media_root, fake_probe):
    make_files(media_root, "P/Show 7/a.mp4", "P/Show 07/b.mp4", "P/Show 7/c.mp4", "P/Show 07/d.mp4")
    lib = create_library(conn, media_root, "Media", str(media_root))
    scan_library(conn, lib, probe_fn=fake_probe)
    assert [i["rel_path"] for i in browse(conn, lib, "P", show_all=True)["items"]] == [
        "P/Show 07/b.mp4", "P/Show 07/d.mp4", "P/Show 7/a.mp4", "P/Show 7/c.mp4",
    ]


def test_show_all_hides_missing_videos(conn, media_root, fake_probe):
    make_files(media_root, "A/a.mp4", "A/B/b.mp4")
    lib = create_library(conn, media_root, "Media", str(media_root))
    scan_library(conn, lib, probe_fn=fake_probe)
    (media_root / "A/B/b.mp4").unlink()
    scan_library(conn, lib, probe_fn=fake_probe)
    assert [i["title"] for i in browse(conn, lib, "A", show_all=True)["items"]] == ["a"]


def neighbors(client, item, path="Many"):
    return client.get(f"/api/items/{item}/neighbors", params={"path": path})


def test_neighbors_step_through_the_show_all_list(client, big):
    items = get(client, big, all="true")["items"]
    ids = [i["id"] for i in items]
    first = neighbors(client, ids[0]).json()
    assert first["position"] == 1 and first["total"] == 26 and first["prev"] is None
    assert first["next"] == {"kind": "video", "id": ids[1], "title": items[1]["title"], "rel_path": items[1]["rel_path"]}
    middle = neighbors(client, ids[12]).json()
    assert middle["position"] == 13 and middle["prev"]["id"] == ids[11] and middle["next"]["id"] == ids[13]
    last = neighbors(client, ids[-1]).json()
    assert last["position"] == 26 and last["next"] is None and last["prev"]["id"] == ids[-2]


def test_prev_and_next_walk_the_list_with_its_clips(client, big):
    """Next from a video goes to its first clip, and on through its clips to the
    next video; prev walks back the same way. Positions and the total count
    clips too: the list is the one Show all shows."""
    videos = {i["rel_path"]: i["id"] for i in get(client, big, all="true")["items"]}
    for path, marks in [("Many/clip1.mp4", [(1, 2)]), ("Many/clip2.mp4", [(1, 2), (5, 6)]),
                        ("Many/Sub/deep.mp4", [(7, 9)])]:
        for start, end in marks:
            client.post(f"/api/items/{videos[path]}/clips", json={"start": start, "end": end})
    shown = [(i.get("kind", "video"), i["id"]) for i in get(client, big, all="true", limit=500)["items"]]
    assert len(shown) == 30

    def around(kind, uid):
        url = f"/api/{'clips' if kind == 'clip' else 'items'}/{uid}/neighbors"
        res = client.get(url, params={"path": "Many"})
        assert res.status_code == 200, res.text
        return res.json()

    walked, entry = [], {"kind": "video", "id": shown[0][1]}
    while entry:
        here = around(entry["kind"], entry["id"])
        walked.append((entry["kind"], entry["id"]))
        assert here["position"] == len(walked) and here["total"] == 30
        entry = here["next"]
    assert walked == shown
    back, entry = [], {"kind": shown[-1][0], "id": shown[-1][1]}
    while entry:
        back.append((entry["kind"], entry["id"]))
        entry = around(entry["kind"], entry["id"])["prev"]
    assert back == shown[::-1]
    # A clip's entry names it, its times, and its video.
    clip = around("video", videos["Many/clip2.mp4"])["next"]
    assert clip["kind"] == "clip" and clip["name"] == "Clip 1" and clip["video_id"] == videos["Many/clip2.mp4"]
    assert (clip["start"], clip["end"], clip["title"], clip["rel_path"]) == (1.0, 2.0, "clip2", "Many/clip2.mp4")
    # Not in the list: a clip whose video isn't below the folder, or no such clip.
    [deep_clip] = client.get(f"/api/items/{videos['Many/Sub/deep.mp4']}/clips").json()
    assert client.get(f"/api/clips/{deep_clip['id']}/neighbors", params={"path": "Many/Sub"}).json()["total"] == 2
    assert client.get(f"/api/clips/{shown[1][1]}/neighbors", params={"path": "Many/Sub"}).status_code == 404
    assert client.get("/api/clips/nope/neighbors", params={"path": "Many"}).status_code == 404

def test_neighbors_of_a_video_outside_the_list(client, big):
    deep = next(i for i in get(client, big, all="true")["items"] if i["rel_path"] == "Many/Sub/deep.mp4")
    assert neighbors(client, deep["id"], path="Many/Sub").json()["total"] == 1
    outside = get(client, big, all="true")["items"][0]["id"]
    assert neighbors(client, outside, path="Many/Sub").status_code == 404   # not below that folder
    assert neighbors(client, "nope").status_code == 404
    assert neighbors(client, outside, path="").json()["total"] == 26       # the library's top


def test_neighbors_skip_missing_videos(conn, media_root, fake_probe):
    from reel.browse import neighbors as around
    make_files(media_root, "A/1.mp4", "A/2.mp4", "A/3.mp4")
    lib = create_library(conn, media_root, "Media", str(media_root))
    scan_library(conn, lib, probe_fn=fake_probe)
    (media_root / "A/2.mp4").unlink()
    scan_library(conn, lib, probe_fn=fake_probe)
    ids = {i["rel_path"]: i["id"] for i in browse(conn, lib, "A", show_all=True)["items"]}
    result = around(conn, ids["A/1.mp4"], "A")
    assert result["next"]["rel_path"] == "A/3.mp4" and result["total"] == 2


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
    r = conn.execute("SELECT parent_dir, title_key, path_key, title FROM media_items WHERE rel_path = ?",
                     (rel_path,)).fetchone()
    return dict(r) if r else None


def test_scan_fills_folder_and_order(conn, media_root, fake_probe):
    make_files(media_root, "A/B/Clip 7.mp4", "top.mp4")
    lib = create_library(conn, media_root, "Media", str(media_root))
    scan_library(conn, lib, probe_fn=fake_probe)
    row = columns(conn, "A/B/Clip 7.mp4")
    assert row["parent_dir"] == "A/B" and row["title_key"] == sort_key(row["title"], "A/B/Clip 7.mp4")
    assert columns(conn, "top.mp4")["parent_dir"] == ""
    assert row["path_key"] == path_key("A/B/Clip 7.mp4")


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
    assert row["path_key"] == path_key("New/renamed.mp4")
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


def test_upgrade_fills_path_order(tmp_path, monkeypatch):
    path = tmp_path / "reel.db"
    monkeypatch.setattr(db, "MIGRATIONS", [m for m in db.MIGRATIONS if m[0] < 9])
    init_db(path)
    conn = connect(path)
    conn.execute("INSERT INTO libraries (uid, name, path) VALUES ('l', 'L', '/m')")
    for rel in ("Show/clip10.mp4", "Show/Sub/clip2.mp4", "loose.mp4"):
        conn.execute("INSERT INTO media_items (uid, library_id, rel_path, title, size, mtime, parent_dir) "
                     "VALUES (?, 1, ?, ?, 1, 1, ?)", (rel, rel, rel, parent_dir(rel)))
    conn.commit()
    conn.close()
    monkeypatch.undo()
    init_db(path)
    conn = connect(path)
    assert columns(conn, "loose.mp4")["path_key"] == path_key("loose.mp4")
    assert [i["rel_path"] for i in browse(conn, 1, "Show", show_all=True)["items"]] == [
        "Show/clip10.mp4", "Show/Sub/clip2.mp4"]


def test_upgrade_recomputes_path_order_by_folder(tmp_path, monkeypatch):
    path = tmp_path / "reel.db"
    monkeypatch.setattr(db, "MIGRATIONS", [m for m in db.MIGRATIONS if m[0] < 10])
    init_db(path)
    conn = connect(path)
    conn.execute("INSERT INTO libraries (uid, name, path) VALUES ('l', 'L', '/m')")
    for rel in ("P/Show 7/a.mp4", "P/Show 07/b.mp4", "P/Show 7/c.mp4"):
        # A version 9 key: case and padding folded across the whole path.
        old = rel.lower().replace("07", "7").replace("/", "\x01") + "\x00" + rel
        conn.execute("INSERT INTO media_items (uid, library_id, rel_path, title, size, mtime, parent_dir, path_key) "
                     "VALUES (?, 1, ?, ?, 1, 1, ?, ?)", (rel, rel, rel, parent_dir(rel), old))
    conn.commit()
    conn.close()
    monkeypatch.undo()
    init_db(path)
    conn = connect(path)
    assert columns(conn, "P/Show 7/a.mp4")["path_key"] == path_key("P/Show 7/a.mp4")
    assert [i["rel_path"] for i in browse(conn, 1, "P", show_all=True)["items"]] == [
        "P/Show 07/b.mp4", "P/Show 7/a.mp4", "P/Show 7/c.mp4"]


def test_browsing_uses_the_index(conn):
    def plan(sql, *args):
        return " ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, args))

    here = "library_id = ? AND parent_dir = ? AND missing_since IS NULL"
    assert "media_items_browse" in plan(f"SELECT * FROM media_items WHERE {here} ORDER BY title_key LIMIT 5", 1, "A")
    assert "TEMP B-TREE" not in plan(f"SELECT * FROM media_items WHERE {here} ORDER BY title_key LIMIT 5", 1, "A")
    below = plan("SELECT parent_dir, COUNT(*) FROM media_items WHERE library_id = ? AND parent_dir >= ? "
                 "AND parent_dir < ? AND missing_since IS NULL GROUP BY parent_dir", 1, "A/", "A0")
    assert "media_items_browse" in below and "TEMP B-TREE" not in below
    everything = plan("SELECT * FROM media_items WHERE library_id = ? AND missing_since IS NULL "
                      "AND path_key >= ? AND path_key < ? ORDER BY path_key LIMIT 5", 1, *folder_range("A"))
    assert "media_items_paths" in everything and "TEMP B-TREE" not in everything
