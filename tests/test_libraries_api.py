"""Adding, renaming and removing libraries, and the folder picker."""
from conftest import make_files


def add(client, name, path):
    return client.post("/api/libraries", json={"name": name, "path": str(path)})


def test_add_library(client, media_root):
    (media_root / "Tapes").mkdir()
    res = add(client, "Tapes", media_root / "Tapes")
    assert res.status_code == 201
    body = res.json()
    assert body["name"] == "Tapes"
    assert body["path"] == str(media_root / "Tapes")
    assert body["item_count"] == 0
    assert body["last_scan_at"] is None
    assert body["scan"] is None
    assert [lib["name"] for lib in client.get("/api/libraries").json()] == ["Tapes"]


def test_adding_a_library_does_not_scan_it(client, media_root, fake_probe):
    make_files(media_root, "Tapes/1992.zoo-trip.mpg")
    add(client, "Tapes", media_root / "Tapes")
    client.scans.wait_idle()
    assert fake_probe.calls == []
    assert client.get("/api/libraries").json()[0]["item_count"] == 0


def test_libraries_listed_by_name(client, media_root):
    for name in ("b", "a", "C"):
        (media_root / name).mkdir()
        add(client, name, media_root / name)
    assert [lib["name"] for lib in client.get("/api/libraries").json()] == ["a", "b", "C"]


def test_name_is_trimmed_and_required(client, media_root):
    (media_root / "x").mkdir()
    assert add(client, "   ", media_root / "x").status_code == 400
    assert add(client, "  Home Movies ", media_root / "x").json()["name"] == "Home Movies"


def test_duplicate_name_rejected_case_insensitively(client, media_root):
    (media_root / "a").mkdir()
    (media_root / "b").mkdir()
    add(client, "Movies", media_root / "a")
    res = add(client, "movies", media_root / "b")
    assert res.status_code == 400
    assert "already exists" in res.json()["detail"]


def test_missing_folder_rejected(client, media_root):
    res = add(client, "Nope", media_root / "does-not-exist")
    assert res.status_code == 400
    assert res.json()["detail"] == "Folder not found."


def test_file_instead_of_folder_rejected(client, media_root):
    make_files(media_root, "clip.mp4")
    assert add(client, "Clip", media_root / "clip.mp4").status_code == 400


def test_folder_outside_media_root_rejected(client, media_root, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    res = add(client, "Outside", outside)
    assert res.status_code == 400
    assert "inside" in res.json()["detail"]
    # ../ tricks resolve to the same place and are rejected too.
    assert add(client, "Sneaky", f"{media_root}/../elsewhere").status_code == 400


def test_same_folder_twice_rejected(client, media_root):
    (media_root / "Tapes").mkdir()
    add(client, "Tapes", media_root / "Tapes")
    res = add(client, "Tapes again", f"{media_root}/Tapes/")
    assert res.status_code == 400
    assert "already" in res.json()["detail"]


def test_overlapping_folders_rejected(client, media_root):
    (media_root / "Personal/Tapes").mkdir(parents=True)
    add(client, "Tapes", media_root / "Personal/Tapes")
    res = add(client, "Personal", media_root / "Personal")
    assert res.status_code == 400
    assert "contains" in res.json()["detail"]

    (media_root / "Films/Collection").mkdir(parents=True)
    add(client, "Films", media_root / "Films")
    res = add(client, "Collection", media_root / "Films/Collection")
    assert res.status_code == 400
    assert "inside" in res.json()["detail"]


def test_sibling_with_shared_prefix_is_not_overlap(client, media_root):
    (media_root / "Tapes").mkdir()
    (media_root / "Tapes2").mkdir()
    add(client, "Tapes", media_root / "Tapes")
    assert add(client, "Tapes2", media_root / "Tapes2").status_code == 201


def test_rename_library(client, media_root):
    (media_root / "a").mkdir()
    (media_root / "b").mkdir()
    lib = add(client, "Old", media_root / "a").json()
    add(client, "Taken", media_root / "b")

    res = client.patch(f"/api/libraries/{lib['id']}", json={"name": "New"})
    assert res.status_code == 200 and res.json()["name"] == "New"
    # Changing only the case of its own name is fine.
    assert client.patch(f"/api/libraries/{lib['id']}", json={"name": "NEW"}).status_code == 200
    assert client.patch(f"/api/libraries/{lib['id']}", json={"name": "taken"}).status_code == 400
    assert client.patch(f"/api/libraries/{lib['id']}", json={"name": ""}).status_code == 400
    assert client.patch("/api/libraries/999", json={"name": "x"}).status_code == 404


def test_remove_library_keeps_files(client, media_root):
    make_files(media_root, "Tapes/1992.zoo-trip.mpg")
    lib = add(client, "Tapes", media_root / "Tapes").json()
    client.post(f"/api/libraries/{lib['id']}/scan")
    client.scans.wait_idle()

    assert client.delete(f"/api/libraries/{lib['id']}").status_code == 204
    assert client.get("/api/libraries").json() == []
    assert (media_root / "Tapes/1992.zoo-trip.mpg").exists()
    assert client.delete(f"/api/libraries/{lib['id']}").status_code == 404
    # The folder can be added again afterwards.
    assert add(client, "Tapes", media_root / "Tapes").status_code == 201


def test_folder_picker(client, media_root):
    make_files(media_root, "Personal/Tapes/a.mpg", "Personal/Camcorder/clip01.avi", "Films/x.mp4", "loose.mp4")
    (media_root / ".zfs").mkdir()

    root = client.get("/api/folders").json()
    assert root["path"] == str(media_root)
    assert root["parent"] is None
    assert [f["name"] for f in root["folders"]] == ["Films", "Personal"]

    internal = client.get("/api/folders", params={"path": root["folders"][1]["path"]}).json()
    assert [f["name"] for f in internal["folders"]] == ["Camcorder", "Tapes"]
    assert internal["parent"] == str(media_root)


def test_folder_picker_stays_inside_media_root(client, media_root):
    assert client.get("/api/folders", params={"path": "/etc"}).status_code == 400
    assert client.get("/api/folders", params={"path": f"{media_root}/.."}).status_code == 400
    assert client.get("/api/folders", params={"path": f"{media_root}/missing"}).status_code == 400


def test_home_page_served(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "Libraries" in res.text


def test_frontend_files_are_revalidated(client):
    """Browsers must pick up a new version of the app without a hard refresh."""
    for path in ("/", "/app.js", "/api.js", "/style.css"):
        res = client.get(path)
        assert res.status_code == 200
        assert res.headers["cache-control"] == "no-cache"
    etag = client.get("/api.js").headers["etag"]
    assert client.get("/api.js", headers={"If-None-Match": etag}).status_code == 304


def test_unwritable_data_folder_gives_a_clear_error(tmp_path, media_root):
    import os
    import pytest
    from reel.config import Settings
    from reel.main import create_app

    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)  # read-only, like a root-owned bind mount
    try:
        if os.access(locked, os.W_OK):
            pytest.skip("running as root: permissions aren't enforced")
        with pytest.raises(SystemExit, match="can't write to its data folder"):
            create_app(Settings(media_root=media_root, data_dir=locked))
    finally:
        locked.chmod(0o700)


def test_health(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["ffmpeg"]  # the installed ffmpeg's version


def test_health_checks_stay_out_of_the_access_log(client):
    import logging

    access = logging.getLogger("uvicorn.access")
    record = lambda path: logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "GET %s HTTP/1.1" 200', ("127.0.0.1", path), None)
    assert not access.filter(record("/api/health"))
    assert access.filter(record("/api/libraries"))


def test_favicon(client):
    res = client.get("/favicon.svg")
    assert res.status_code == 200 and "svg" in res.headers["content-type"]
    assert 'rel="icon"' in client.get("/").text


def test_missing_grace_from_environment(monkeypatch):
    from datetime import timedelta
    from reel.config import Settings

    monkeypatch.setenv("REEL_MISSING_GRACE_DAYS", "0.5")
    assert Settings.from_env().missing_grace == timedelta(hours=12)
    monkeypatch.delenv("REEL_MISSING_GRACE_DAYS")
    assert Settings.from_env().missing_grace == timedelta(days=7)
