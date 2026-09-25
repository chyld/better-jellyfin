"""Uploads and clean-up must never lose a just-saved picture."""
import os
import subprocess
import time

import pytest

from reel import custom_images, tags
from reel.db import connect

from conftest import make_files, requires_ffmpeg

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
    real_save = custom_images.save_upload

    def save_then_cleanup(data, out, **kwargs):
        real_save(data, out, **kwargs)
        conn = connect(settings.db_path)
        try:
            custom_images.prune(conn, settings.images_dir)
        finally:
            conn.close()

    monkeypatch.setattr(custom_images, "save_upload", save_then_cleanup)
    monkeypatch.setattr(tags, "save_upload", save_then_cleanup)


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
    video = video_id(client, lib)
    first = client.put(f"/api/items/{video}/image", content=picture).json()["custom_image"]
    second = client.put(f"/api/items/{video}/image", content=picture).json()["custom_image"]
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
    assert custom_images.prune(conn, settings.images_dir) == 1
    assert fresh.exists() and not stale.exists()


def test_old_style_file_names_are_adopted(client, lib, picture, settings):
    """Images saved as "<uuid>.jpg" by older versions are renamed, not lost."""
    video = video_id(client, lib)
    version = client.put(f"/api/items/{video}/image", content=picture).json()["custom_image"]
    folder = settings.images_dir / "videos"
    (folder / f"{video}-{version}.jpg").rename(folder / f"{video}.jpg")  # as an older Reel stored it
    conn = connect(settings.db_path)
    try:
        custom_images.adopt_unversioned_files(conn, settings.images_dir)
    finally:
        conn.close()
    assert (folder / f"{video}-{version}.jpg").exists()
    assert client.get(f"/api/items/{video}/thumb").status_code == 200


def test_two_first_uploads_to_a_folder_leave_a_working_picture(client, lib, picture, settings, monkeypatch):
    """A second first-upload lands while the first is saving its file."""
    real_save = custom_images.save_upload
    lib_pk = connect(settings.db_path).execute("SELECT id FROM libraries").fetchone()[0]
    raced = []

    def save_then_race(data, out, **kwargs):
        real_save(data, out, **kwargs)
        if not raced:
            raced.append(True)
            other = connect(settings.db_path)
            try:
                custom_images.set_folder_image(other, settings.images_dir, lib_pk, "Tapes", data)
            finally:
                other.close()

    monkeypatch.setattr(custom_images, "save_upload", save_then_race)
    assert client.put(f"/api/libraries/{lib}/folder-image", params={"path": "Tapes"}, content=picture).status_code == 200
    conn = connect(settings.db_path)
    row = custom_images.custom_folder_image(conn, lib_pk, "Tapes")
    assert custom_images.folder_image_path(settings.images_dir, row["uid"], row["version"]).is_file()
