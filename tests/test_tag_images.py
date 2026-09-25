"""Uploading an image for a tag, shown on its card on Home."""
import sqlite3
import subprocess

import pytest

from reel.db import connect, init_db
from reel.images import MAX_UPLOAD_BYTES, image_kind

from conftest import make_files, requires_ffmpeg


def make_image(tmp_path, name, size="1600x900", color="orange"):
    path = tmp_path / name
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={color}:s={size}", "-frames:v", "1", str(path)],
        check=True,
    )
    return path.read_bytes()


def image_size(data, tmp_path):
    f = tmp_path / "check.jpg"
    f.write_bytes(data)
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(f)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return tuple(int(x) for x in out.split(","))


@pytest.fixture
def tags(client, media_root):
    """Two videos with tags; returns {tag name: tag id}."""
    make_files(media_root, "Tapes/a.mpg", "Tapes/b.mpg")
    lib = client.post("/api/libraries", json={"name": "Tapes", "path": str(media_root / "Tapes")}).json()
    client.post(f"/api/libraries/{lib['id']}/scan")
    client.scans.wait_idle()
    items = client.get(f"/api/libraries/{lib['id']}/browse").json()["items"]
    for item, name in ((items[0], "family"), (items[1], "family"), (items[0], "vhs"), (items[1], "road-trip")):
        client.post(f"/api/items/{item['id']}/tags", json={"name": name})
    return {t["name"]: t["id"] for t in client.get("/api/tags").json()}


def upload(client, tag_id, data):
    return client.put(f"/api/tags/{tag_id}/image", content=data)


def listed(client, name):
    return next(t for t in client.get("/api/tags").json() if t["name"] == name)


def test_tags_start_without_images(client, tags):
    assert listed(client, "family")["image"] is None
    assert client.get(f"/api/tags/{tags['family']}/image").status_code == 404


@requires_ffmpeg
@pytest.mark.parametrize("name", ["pic.png", "pic.jpg", "pic.webp", "pic.gif", "pic.bmp"])
def test_upload_an_image(client, tags, tmp_path, name):
    res = upload(client, tags["family"], make_image(tmp_path, name))
    assert res.status_code == 200
    body = res.json()
    assert body["name"] == "family" and body["count"] == 2 and body["image"]
    assert listed(client, "family")["image"] == body["image"]

    img = client.get(f"/api/tags/{tags['family']}/image")
    assert img.status_code == 200
    assert img.headers["content-type"] == "image/jpeg"
    assert image_size(img.content, tmp_path) == (800, 450)  # shrunk to 800 wide


@requires_ffmpeg
def test_small_images_are_not_enlarged(client, tags, tmp_path):
    upload(client, tags["family"], make_image(tmp_path, "small.png", size="300x300"))
    assert image_size(client.get(f"/api/tags/{tags['family']}/image").content, tmp_path) == (300, 300)


@requires_ffmpeg
def test_replacing_an_image(client, tags, tmp_path):
    first = upload(client, tags["family"], make_image(tmp_path, "a.png", color="red")).json()["image"]
    old = client.get(f"/api/tags/{tags['family']}/image").content
    second = upload(client, tags["family"], make_image(tmp_path, "b.png", color="blue")).json()["image"]
    assert second != first  # new version, so browsers fetch the new picture
    assert client.get(f"/api/tags/{tags['family']}/image").content != old


@requires_ffmpeg
@pytest.mark.parametrize(
    "data",
    [b"hello, this is text", b"%PDF-1.7 not an image", b"\x89PNG\r\n\x1a\n" + b"broken" * 50, b"\xff\xd8\xff" + b"x" * 100],
)
def test_bad_files_are_rejected(client, tags, tmp_path, data):
    good = make_image(tmp_path, "good.png")
    upload(client, tags["family"], good)
    before = client.get(f"/api/tags/{tags['family']}/image").content

    res = upload(client, tags["family"], data)
    assert res.status_code == 400
    assert res.json()["detail"]
    # A failed upload leaves the old image in place.
    assert client.get(f"/api/tags/{tags['family']}/image").content == before


def test_empty_upload(client, tags):
    assert upload(client, tags["family"], b"").status_code == 400


def test_too_large(client, tags):
    res = upload(client, tags["family"], b"\x89PNG\r\n\x1a\n" + b"\0" * MAX_UPLOAD_BYTES)
    assert res.status_code == 413
    assert "20 MB" in res.json()["detail"]


@requires_ffmpeg
def test_remove_an_image(client, tags, tmp_path, settings):
    upload(client, tags["family"], make_image(tmp_path, "a.png"))
    res = client.delete(f"/api/tags/{tags['family']}/image")
    assert res.status_code == 200 and res.json()["image"] is None
    assert client.get(f"/api/tags/{tags['family']}/image").status_code == 404
    assert list(settings.tag_images_dir.glob("*.jpg")) == []


@requires_ffmpeg
def test_deleting_a_tag_deletes_its_image(client, tags, tmp_path, settings):
    upload(client, tags["family"], make_image(tmp_path, "a.png"))
    client.delete(f"/api/tags/{tags['family']}")
    assert list(settings.tag_images_dir.glob("*.jpg")) == []


@requires_ffmpeg
def test_merge_gives_the_survivor_an_image_if_it_had_none(client, tags, tmp_path, settings):
    upload(client, tags["vhs"], make_image(tmp_path, "a.png"))
    picture = client.get(f"/api/tags/{tags['vhs']}/image").content
    merged = client.patch(f"/api/tags/{tags['vhs']}", json={"name": "family"}).json()
    assert merged["merged"] and merged["image"]
    assert client.get(f"/api/tags/{tags['family']}/image").content == picture
    assert len(list(settings.tag_images_dir.glob("*.jpg"))) == 1


@requires_ffmpeg
def test_merge_keeps_the_survivors_own_image(client, tags, tmp_path, settings):
    upload(client, tags["vhs"], make_image(tmp_path, "a.png", color="red"))
    upload(client, tags["family"], make_image(tmp_path, "b.png", color="blue"))
    keep = client.get(f"/api/tags/{tags['family']}/image").content
    client.patch(f"/api/tags/{tags['vhs']}", json={"name": "family"})
    assert client.get(f"/api/tags/{tags['family']}/image").content == keep
    assert len(list(settings.tag_images_dir.glob("*.jpg"))) == 1  # the merged tag's image is gone


def test_unknown_tag(client):
    assert client.put("/api/tags/nope/image", content=b"x").status_code == 404
    assert client.delete("/api/tags/nope/image").status_code == 404
    assert client.get("/api/tags/nope/image").status_code == 404


@pytest.mark.parametrize(
    "data, kind",
    [
        (b"\xff\xd8\xff\xe0rest", "jpeg"), (b"\x89PNG\r\n\x1a\nrest", "png"), (b"GIF89arest", "gif"),
        (b"RIFF\0\0\0\0WEBPrest", "webp"), (b"BMrest", "bmp"),
        (b"RIFF\0\0\0\0WAVErest", None), (b"<svg></svg>", None), (b"", None),
    ],
)
def test_image_kind(data, kind):
    assert image_kind(data) == kind


def test_old_database_gets_tag_images(tmp_path):
    db = tmp_path / "old.db"
    old = sqlite3.connect(db)
    old.execute("CREATE TABLE tags (id INTEGER PRIMARY KEY, uid TEXT NOT NULL UNIQUE, "
                "name TEXT NOT NULL UNIQUE COLLATE NOCASE, created_at TEXT NOT NULL DEFAULT (datetime('now')))")
    old.execute("INSERT INTO tags (uid, name) VALUES ('u1', 'family')")
    old.commit()
    old.close()
    init_db(db)
    conn = connect(db)
    assert conn.execute("SELECT name, image_version FROM tags").fetchone()[:] == ("family", None)
