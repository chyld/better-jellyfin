"""Browsing folders and video details (catalog only, ffprobe faked out)."""
import shutil

import pytest

from conftest import make_files

LAYOUT = [
    "Personal/Camcorder/clip1.avi", "Personal/Camcorder/clip2.avi", "Personal/Camcorder/clip10.avi",
    "Personal/Camcorder/clip1.png", "Personal/Camcorder/folder.png",
    "Personal/Tapes/1996.beach-1.mpg", "Personal/Tapes/1992.zoo-trip.mpg",
    "Personal/Tapes/interview.mov", "Personal/Tapes/1990.picnic-2.mpg",
    "Personal/loose.mp4",
    "Films/Classics/0360/movie.mp4", "Films/Classics/0360/movie.png",
    "Films/Classics/0305/movie.mp4",
    "Films/Classics/03100/movie.mkv",
]


@pytest.fixture
def lib(client, media_root):
    make_files(media_root, *LAYOUT)
    lib = client.post("/api/libraries", json={"name": "Media", "path": str(media_root)}).json()
    client.post(f"/api/libraries/{lib['id']}/scan")
    client.scans.wait_idle()
    return lib["id"]


def browse(client, lib, path="", **params):
    res = client.get(f"/api/libraries/{lib}/browse", params={"path": path, **params})
    assert res.status_code == 200, res.text
    return res.json()


def titles(page):
    return [i["title"] for i in page["items"]]


def test_library_root_lists_top_folders(client, lib):
    page = browse(client, lib)
    assert [f["name"] for f in page["folders"]] == ["Films", "Personal"]
    assert [f["item_count"] for f in page["folders"]] == [3, 8]  # counts include subfolders
    assert page["items"] == []
    assert page["breadcrumbs"] == [{"name": "Media", "path": ""}]


def test_folder_with_videos_and_subfolders(client, lib):
    page = browse(client, lib, "Personal")
    assert [f["name"] for f in page["folders"]] == ["Camcorder", "Tapes"]
    assert [f["path"] for f in page["folders"]] == ["Personal/Camcorder", "Personal/Tapes"]
    assert titles(page) == ["loose"]
    assert page["breadcrumbs"] == [{"name": "Media", "path": ""}, {"name": "Personal", "path": "Personal"}]


def test_numbers_sort_naturally(client, lib):
    assert titles(browse(client, lib, "Personal/Camcorder")) == ["clip1", "clip2", "clip10"]
    folders = browse(client, lib, "Films/Classics")["folders"]
    assert [f["name"] for f in folders] == ["0305", "0360", "03100"]


def test_sort_by_year_puts_undated_last(client, lib):
    page = browse(client, lib, "Personal/Tapes", sort="year")
    assert titles(page) == ["picnic-2", "zoo-trip", "beach-1", "interview"]
    assert page["sort"] == "year"
    assert titles(browse(client, lib, "Personal/Tapes", sort="name")) == [
        "beach-1", "interview", "picnic-2", "zoo-trip",
    ]
    assert browse(client, lib, "Personal/Tapes", sort="bogus")["sort"] == "name"


def test_item_fields(client, lib):
    item = browse(client, lib, "Personal/Tapes", sort="year")["items"][1]
    assert item == {
        "id": item["id"], "title": "zoo-trip", "year": 1992, "duration": 60.0,
        "width": 720, "height": 480, "has_poster": False, "custom_image": None,
    }


def test_folder_and_video_previews(client, lib):
    folders = {f["name"]: f for f in browse(client, lib, "Personal")["folders"]}
    assert folders["Camcorder"]["has_art"] is True   # has folder.png
    assert folders["Tapes"]["has_art"] is False    # gets the folder placeholder
    assert set(folders["Tapes"]) == {"name", "path", "item_count", "has_art", "custom_art"}

    videos = {i["title"]: i["has_poster"] for i in browse(client, lib, "Personal/Camcorder")["items"]}
    assert videos == {"clip1": True, "clip2": False, "clip10": False}  # only clip1.png exists

    # A folder whose videos have previews still gets the folder placeholder.
    classics = {f["name"]: f for f in browse(client, lib, "Films")["folders"]}["Classics"]
    assert classics["has_art"] is False


def test_library_reports_its_own_folder_preview(client, media_root):
    make_files(media_root, "A/folder.png", "A/x/clip.mp4", "B/x/folder.png", "B/x/clip.mp4")
    for name in ("A", "B"):
        client.post("/api/libraries", json={"name": name, "path": str(media_root / name)})
    client.post("/api/libraries/scan")
    client.scans.wait_idle()
    has_art = {lib["name"]: lib["has_art"] for lib in client.get("/api/libraries").json()}
    # Only the library folder's own folder.png counts, not one in a subfolder.
    assert has_art == {"A": True, "B": False}


def test_per_video_folders(client, lib):
    folders = browse(client, lib, "Films/Classics")["folders"]
    assert [(f["name"], f["item_count"]) for f in folders] == [("0305", 1), ("0360", 1), ("03100", 1)]
    assert titles(browse(client, lib, "Films/Classics/0360")) == ["0360"]


def test_path_is_normalised(client, lib):
    assert browse(client, lib, "/Personal/Tapes/")["path"] == "Personal/Tapes"
    assert browse(client, lib, "Personal//./Tapes")["path"] == "Personal/Tapes"


@pytest.mark.parametrize("path", ["Nope", "Personal/Camcorder/clip1.avi", "../etc", "Personal/../../x", "Intern"])
def test_unknown_folders_404(client, lib, path):
    assert client.get(f"/api/libraries/{lib}/browse", params={"path": path}).status_code == 404


def test_unknown_library_404(client):
    assert client.get("/api/libraries/99/browse").status_code == 404


def test_empty_library_root(client, media_root):
    (media_root / "empty").mkdir()
    lib = client.post("/api/libraries", json={"name": "Empty", "path": str(media_root / "empty")}).json()
    page = browse(client, lib["id"])
    assert page["folders"] == [] and page["items"] == []


def test_browsing_does_not_touch_the_disk(client, lib, media_root):
    shutil.rmtree(media_root / "Personal")  # e.g. the NAS is offline
    assert titles(browse(client, lib, "Personal/Camcorder")) == ["clip1", "clip2", "clip10"]


def test_item_detail(client, lib):
    item_id = browse(client, lib, "Personal/Tapes", sort="year")["items"][1]["id"]
    item = client.get(f"/api/items/{item_id}").json()
    assert item["title"] == "zoo-trip"
    assert item["rel_path"] == "Personal/Tapes/1992.zoo-trip.mpg"
    assert item["folder"] == "Personal/Tapes"
    assert item["library_name"] == "Media"
    assert (item["video_codec"], item["audio_codec"], item["interlaced"]) == ("mpeg2video", "mp2", True)
    assert item["has_poster"] is False
    assert [c["name"] for c in item["breadcrumbs"]] == ["Media", "Personal", "Tapes"]


def test_item_detail_at_library_root(client, media_root):
    make_files(media_root, "Tapes/a.mpg")
    lib = client.post("/api/libraries", json={"name": "Tapes", "path": str(media_root / "Tapes")}).json()
    client.post(f"/api/libraries/{lib['id']}/scan")
    client.scans.wait_idle()
    item_id = browse(client, lib["id"])["items"][0]["id"]
    item = client.get(f"/api/items/{item_id}").json()
    assert item["folder"] == ""
    assert item["breadcrumbs"] == [{"name": "Tapes", "path": ""}]


def test_unknown_item_404(client):
    assert client.get("/api/items/12345").status_code == 404
    assert client.get("/api/items/12345/thumb").status_code == 404
