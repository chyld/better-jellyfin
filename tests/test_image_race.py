"""Uploads and clean-up must never lose a just-saved picture."""
import os
import subprocess
import time

import pytest

from reel import pictures
from reel.db import connect

from conftest import make_files, requires_ffmpeg, settle_pictures

pytestmark = requires_ffmpeg


@pytest.fixture
def picture(tmp_path):
    path = tmp_path / "p.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=teal:s=320x180",
                    "-frames:v", "1", str(path)], check=True)
    return path.read_bytes()


@pytest.fixture
def lib(client, media_root):
    make_files(media_root, "Tapes/a.mpg", "Tapes/b.mpg")
    lib = client.post("/api/libraries", json={"name": "Media", "path": str(media_root)}).json()["id"]
    client.post(f"/api/libraries/{lib}/scan")
    client.scans.wait_idle()
    return lib


def video_id(client, lib):
    items = client.get(f"/api/libraries/{lib}/browse", params={"path": "Tapes"}).json()["items"]
    return next(i["id"] for i in items if i["title"] == "a")


def cleanup_during_upload(monkeypatch, settings):
    """Run the clean-up right after an upload writes its file, before it's recorded."""
    real_save = pictures.save_upload

    def save_then_cleanup(data, out, **kwargs):
        real_save(data, out, **kwargs)
        conn = connect(settings.db_path)
        try:
            pictures.prune(conn, settings.images_dir)
        finally:
            conn.close()

    monkeypatch.setattr(pictures, "save_upload", save_then_cleanup)


def test_cleanup_during_a_video_upload_keeps_the_new_picture(client, lib, picture, settings, monkeypatch):
    cleanup_during_upload(monkeypatch, settings)
    video = video_id(client, lib)
    assert client.put(f"/api/items/{video}/image", content=picture).status_code == 200
    assert client.get(f"/api/items/{video}/thumb").status_code == 200


def test_cleanup_during_a_folder_upload_keeps_the_new_picture(client, lib, picture, settings, monkeypatch):
    cleanup_during_upload(monkeypatch, settings)
    assert client.put(f"/api/libraries/{lib}/folder-image", params={"path": "Tapes"}, content=picture).status_code == 200
    assert client.get(f"/api/libraries/{lib}/folder-art", params={"path": "Tapes"}).status_code == 200


def test_cleanup_during_a_tag_upload_keeps_the_new_picture(client, lib, picture, settings, monkeypatch):
    cleanup_during_upload(monkeypatch, settings)
    video = video_id(client, lib)
    tag = client.post(f"/api/items/{video}/tags", json={"name": "family"}).json()[0]
    assert client.put(f"/api/tags/{tag['id']}/image", content=picture).status_code == 200
    assert client.get(f"/api/tags/{tag['id']}/image").status_code == 200


def test_each_upload_gets_its_own_file_and_the_old_one_goes(client, lib, picture, settings):
    """The old file is retired, not deleted at once (a request may be sending it),
    and goes with the clean-up once the grace period has passed."""
    video = video_id(client, lib)
    first = client.put(f"/api/items/{video}/image", content=picture).json()["custom_image"]
    second = client.put(f"/api/items/{video}/image", content=picture).json()["custom_image"]
    assert sorted(p.name for p in (settings.images_dir / "videos").glob("*.jpg")) == sorted(
        [f"{video}-{first}.jpg", f"{video}-{second}.jpg"])
    settle_pictures(settings)
    assert sorted(p.name for p in (settings.images_dir / "videos").glob("*.jpg")) == [f"{video}-{second}.jpg"]
    assert first != second


def test_cleanup_waits_for_the_grace_period(settings, conn, picture):
    folder = settings.images_dir / "videos"
    folder.mkdir(parents=True, exist_ok=True)
    fresh, stale = folder / "x-new.jpg", folder / "y-old.jpg"
    fresh.write_bytes(picture)
    stale.write_bytes(picture)
    old = time.time() - 7200
    os.utime(stale, (old, old))
    assert pictures.prune(conn, settings.images_dir) == 1
    assert fresh.exists() and not stale.exists()


def test_old_style_file_names_are_adopted(client, lib, picture, settings):
    """Images saved as "<uuid>.jpg" by older versions are renamed, not lost."""
    video = video_id(client, lib)
    version = client.put(f"/api/items/{video}/image", content=picture).json()["custom_image"]
    folder = settings.images_dir / "videos"
    (folder / f"{video}-{version}.jpg").rename(folder / f"{video}.jpg")  # as an older Reel stored it
    conn = connect(settings.db_path)
    try:
        pictures.adopt_unversioned_files(conn, settings.images_dir)
    finally:
        conn.close()
    assert (folder / f"{video}-{version}.jpg").exists()
    assert client.get(f"/api/items/{video}/thumb").status_code == 200


def test_two_first_uploads_to_a_folder_leave_a_working_picture(client, lib, picture, settings, monkeypatch):
    """A second first-upload lands while the first is saving its file."""
    real_save = pictures.save_upload
    lib_pk = connect(settings.db_path).execute("SELECT id FROM libraries").fetchone()[0]
    raced = []

    def save_then_race(data, out, **kwargs):
        real_save(data, out, **kwargs)
        if not raced:
            raced.append(True)
            other = connect(settings.db_path)
            try:
                pictures.set_uploaded(other, settings.images_dir, pictures.FolderPicture(lib_pk, "Tapes"), data)
            finally:
                other.close()

    monkeypatch.setattr(pictures, "save_upload", save_then_race)
    assert client.put(f"/api/libraries/{lib}/folder-image", params={"path": "Tapes"}, content=picture).status_code == 200
    conn = connect(settings.db_path)
    assert pictures.picture_file(conn, settings.images_dir, pictures.FolderPicture(lib_pk, "Tapes")) is not None


def test_a_picture_replaced_while_a_request_sends_it_is_still_there(client, lib, picture, settings):
    """A request picks the picture (its file and version, from one read); the picture
    is then replaced, and the clean-up runs, before the request opens the file. The
    file it picked is still there, and still the version it named."""
    from reel import pictures
    from reel.db import connect

    video = video_id(client, lib)
    first = client.put(f"/api/items/{video}/image", content=picture).json()["custom_image"]
    thumbs = client.app.state.thumbnails
    found = thumbs.item_picture(video)                                   # the request picks it...
    assert found["version"] == first and found["file"].name == f"{video}-{first}.jpg"
    second = client.put(f"/api/items/{video}/image", content=picture).json()["custom_image"]   # ...it's replaced...
    conn = connect(settings.db_path)
    pictures.prune(conn, settings.images_dir)                            # ...and cleaned up
    conn.close()
    assert found["file"].read_bytes()                                    # still there to send
    assert thumbs.item_picture(video)["version"] == second
    # A tag's picture the same way.
    tag = client.post(f"/api/items/{video}/tags", json={"name": "family"}).json()[0]
    client.put(f"/api/tags/{tag['id']}/image", content=picture)
    conn = connect(settings.db_path)
    path, _ = pictures.picture_file(conn, settings.images_dir, pictures.TagPicture(tag["id"]))
    conn.close()
    client.delete(f"/api/tags/{tag['id']}/image")
    assert path.read_bytes()


@pytest.mark.parametrize("change", ["replace", "remove"])
def test_an_old_picture_replaced_during_a_cleanup_is_still_there(client, lib, picture, settings, monkeypatch, change):
    """The picture being replaced is hours old, and a clean-up runs right when the
    database stops pointing at it: it was retired first, so the clean-up keeps it
    (a request that picked it can still send it)."""
    import os
    import time

    from reel import pictures
    from reel.db import connect

    video = video_id(client, lib)
    client.put(f"/api/items/{video}/image", content=picture)
    found = client.app.state.thumbnails.item_picture(video)
    old = time.time() - 7200
    os.utime(found["file"], (old, old))                                  # long since uploaded

    real_record = pictures.VideoPicture.record

    def record_then_clean_up(self, conn, file_id, version):
        real_record(self, conn, file_id, version)
        conn.commit()                                                    # the old one is unreferenced now...
        other = connect(settings.db_path)
        pictures.prune(other, settings.images_dir)                       # ...and a clean-up runs
        other.close()

    monkeypatch.setattr(pictures.VideoPicture, "record", record_then_clean_up)
    if change == "replace":
        client.put(f"/api/items/{video}/image", content=picture)
    else:
        client.delete(f"/api/items/{video}/image")
    assert found["file"].read_bytes()
