"""Uploading images for folders and videos that have none on the NAS."""
import subprocess

import pytest

from reel import fetch

from conftest import make_files, requires_ffmpeg

pytestmark = requires_ffmpeg


def png(path, color="orange", size="640x360"):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={color}:s={size}",
                    "-frames:v", "1", str(path)], check=True)
    return path.read_bytes()


@pytest.fixture
def picture(tmp_path):
    return png(tmp_path / "upload.png")


@pytest.fixture
def lib(client, media_root):
    """A library: Tapes has no images; Lectures has folder.png and a video with its own poster."""
    make_files(media_root, "Tapes/a.mpg", "Tapes/b.mpg", "Lectures/x.mp4", "Lectures/y.mp4")
    png(media_root / "Lectures/folder.png", color="blue")
    png(media_root / "Lectures/x.png", color="green")
    res = client.post("/api/libraries", json={"name": "Media", "path": str(media_root)}).json()
    rescan(client, res["id"])
    return res["id"]


def rescan(client, lib_id):
    client.post(f"/api/libraries/{lib_id}/scan")
    client.scans.wait_idle()


def items(client, lib, path):
    return {i["title"]: i for i in client.get(f"/api/libraries/{lib}/browse", params={"path": path}).json()["items"]}


def folders(client, lib, path=""):
    return {f["name"]: f for f in client.get(f"/api/libraries/{lib}/browse", params={"path": path}).json()["folders"]}


def images_on_disk(settings, kind):
    folder = settings.images_dir / kind
    return sorted(p.name for p in folder.glob("*.jpg")) if folder.is_dir() else []


# ---- Videos ---------------------------------------------------------------------------


def test_video_without_image_can_get_one(client, lib, picture, settings):
    video = items(client, lib, "Tapes")["a"]
    assert (video["has_poster"], video["custom_image"]) == (False, None)
    assert client.get(f"/api/items/{video['id']}/thumb").status_code == 404

    res = client.put(f"/api/items/{video['id']}/image", content=picture)
    assert res.status_code == 200
    version = res.json()["custom_image"]
    assert version

    video = items(client, lib, "Tapes")["a"]
    assert (video["has_poster"], video["custom_image"]) == (False, version)
    thumb = client.get(f"/api/items/{video['id']}/thumb")
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/jpeg"
    assert images_on_disk(settings, "videos") == [f"{video['id']}.jpg"]
    assert client.get(f"/api/items/{video['id']}").json()["custom_image"] == version


def test_replace_and_remove_a_video_image(client, lib, tmp_path, settings):
    video = items(client, lib, "Tapes")["a"]["id"]
    first = client.put(f"/api/items/{video}/image", content=png(tmp_path / "1.png", "red")).json()["custom_image"]
    second = client.put(f"/api/items/{video}/image", content=png(tmp_path / "2.png", "blue")).json()["custom_image"]
    assert first != second
    assert client.delete(f"/api/items/{video}/image").json() == {"custom_image": None}
    assert client.get(f"/api/items/{video}/thumb").status_code == 404
    assert images_on_disk(settings, "videos") == []


def test_video_with_a_nas_image_cannot_get_an_upload(client, lib, picture):
    video = items(client, lib, "Lectures")["x"]
    assert video["has_poster"] is True
    res = client.put(f"/api/items/{video['id']}/image", content=picture)
    assert res.status_code == 409
    assert "x.png" in res.json()["detail"]


def test_nas_image_wins_once_it_appears(client, lib, picture, media_root):
    video = items(client, lib, "Tapes")["a"]["id"]
    client.put(f"/api/items/{video}/image", content=picture)
    uploaded = client.get(f"/api/items/{video}/thumb").content
    png(media_root / "Tapes/a.png", color="purple")  # someone adds a poster on the NAS
    rescan(client, lib)
    item = items(client, lib, "Tapes")["a"]
    assert item["has_poster"] is True
    assert client.get(f"/api/items/{video}/thumb").content != uploaded


def test_bad_video_uploads(client, lib):
    video = items(client, lib, "Tapes")["a"]["id"]
    assert client.put(f"/api/items/{video}/image", content=b"not an image").status_code == 400
    assert client.put(f"/api/items/{video}/image", content=b"").status_code == 400
    assert client.put("/api/items/nope/image", content=b"x").status_code == 404


def test_video_image_from_url(client, lib, picture, monkeypatch):
    monkeypatch.setattr(fetch, "fetch_image_bytes", lambda url: picture)
    video = items(client, lib, "Tapes")["a"]["id"]
    res = client.post(f"/api/items/{video}/image-url", json={"url": "https://example.com/a.png"})
    assert res.status_code == 200 and res.json()["custom_image"]


# ---- Folders --------------------------------------------------------------------------


def upload_folder(client, lib, path, data):
    return client.put(f"/api/libraries/{lib}/folder-image", params={"path": path}, content=data)


def test_folder_without_image_can_get_one(client, lib, picture, settings):
    assert folders(client, lib)["Tapes"]["custom_art"] is None
    res = upload_folder(client, lib, "Tapes", picture)
    assert res.status_code == 200
    version = res.json()["custom_art"]
    vhs = folders(client, lib)["Tapes"]
    assert (vhs["has_art"], vhs["custom_art"]) == (False, version)
    art = client.get(f"/api/libraries/{lib}/folder-art", params={"path": "Tapes"})
    assert art.status_code == 200 and art.headers["content-type"] == "image/jpeg"
    assert len(images_on_disk(settings, "folders")) == 1


def test_library_tile_image(client, lib, picture):
    upload_folder(client, lib, "", picture)
    listed = client.get("/api/libraries").json()[0]
    assert listed["has_art"] is False and listed["custom_art"]
    assert client.get(f"/api/libraries/{lib}/folder-art", params={"path": ""}).status_code == 200


def test_replace_and_remove_a_folder_image(client, lib, tmp_path, settings):
    first = upload_folder(client, lib, "Tapes", png(tmp_path / "1.png", "red")).json()["custom_art"]
    second = upload_folder(client, lib, "Tapes", png(tmp_path / "2.png", "blue")).json()["custom_art"]
    assert first != second
    assert len(images_on_disk(settings, "folders")) == 1  # replaced, not added
    res = client.delete(f"/api/libraries/{lib}/folder-image", params={"path": "Tapes"})
    assert res.json() == {"custom_art": None}
    assert folders(client, lib)["Tapes"]["custom_art"] is None
    assert client.get(f"/api/libraries/{lib}/folder-art", params={"path": "Tapes"}).status_code == 404
    assert images_on_disk(settings, "folders") == []


def test_folder_with_nas_image_cannot_get_an_upload(client, lib, picture):
    res = upload_folder(client, lib, "Lectures", picture)
    assert res.status_code == 409
    assert "folder.png" in res.json()["detail"]


@pytest.mark.parametrize("path", ["Nope", "../x", "Tapes/a.mpg"])
def test_unknown_folder(client, lib, picture, path):
    assert upload_folder(client, lib, path, picture).status_code == 404


def test_folder_image_from_url(client, lib, picture, monkeypatch):
    monkeypatch.setattr(fetch, "fetch_image_bytes", lambda url: picture)
    res = client.post(f"/api/libraries/{lib}/folder-image-url", params={"path": "Tapes"},
                      json={"url": "https://example.com/a.png"})
    assert res.status_code == 200 and res.json()["custom_art"]
    res = client.post(f"/api/libraries/{lib}/folder-image-url", params={"path": "Nope"},
                      json={"url": "https://example.com/a.png"})
    assert res.status_code == 404


# ---- Clean-up --------------------------------------------------------------------------


def test_images_of_deleted_videos_and_folders_are_removed(client, lib, picture, media_root, settings):
    video = items(client, lib, "Tapes")["a"]["id"]
    client.put(f"/api/items/{video}/image", content=picture)
    upload_folder(client, lib, "Tapes", picture)
    for name in ("a.mpg", "b.mpg"):
        (media_root / "Tapes" / name).unlink()  # the whole Tapes folder is emptied
    rescan(client, lib)
    # Missing, not yet removed: the images are kept in case the files come back.
    assert len(images_on_disk(settings, "videos")) == 1
    assert len(images_on_disk(settings, "folders")) == 1
    # Once the grace period has passed, the next scan removes them for good.
    import sqlite3
    db = sqlite3.connect(settings.db_path)
    db.execute("UPDATE media_items SET missing_since = datetime('now', '-30 days') WHERE missing_since IS NOT NULL")
    db.commit()
    db.close()
    rescan(client, lib)
    assert images_on_disk(settings, "videos") == []
    assert images_on_disk(settings, "folders") == []


def test_removing_a_library_removes_its_images(client, lib, picture, settings):
    client.put(f"/api/items/{items(client, lib, 'Tapes')['a']['id']}/image", content=picture)
    upload_folder(client, lib, "", picture)
    assert client.delete(f"/api/libraries/{lib}").status_code == 204
    assert images_on_disk(settings, "videos") == [] and images_on_disk(settings, "folders") == []


def test_old_tag_image_folder_is_moved(settings, picture):
    from reel.main import create_app

    old = settings.data_dir / "tag-images"
    old.mkdir(parents=True)
    (old / "rough-cut.jpg").write_bytes(picture)
    create_app(settings)
    assert not old.exists()
    # The tag doesn't exist in this database, so the moved file is then pruned.
    assert images_on_disk(settings, "tags") == []
