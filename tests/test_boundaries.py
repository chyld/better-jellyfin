"""Nothing outside a library folder may be cataloged, served or used as a picture."""
import subprocess

import pytest

from reel.libraries import create_library
from reel.paths import OutsideRoot, resolve_inside
from reel.scanner import scan_library

from conftest import make_files, requires_ffmpeg


@pytest.fixture
def outside(tmp_path):
    """Files that live outside the media root."""
    folder = tmp_path / "private"
    make_files(folder, "secret.mp4", "secret.png", "folder.png")
    (folder / "secret.mp4").write_bytes(b"not for you" * 100)
    return folder


def test_resolve_inside(tmp_path, outside):
    root = tmp_path / "lib"
    make_files(root, "a/ok.mp4")
    (root / "a/escape.mp4").symlink_to(outside / "secret.mp4")
    (root / "a/alias.mp4").symlink_to(root / "a/ok.mp4")
    assert resolve_inside(root, "a/ok.mp4") == (root / "a/ok.mp4").resolve()
    assert resolve_inside(root, "a/alias.mp4") == (root / "a/ok.mp4").resolve()  # symlink inside: fine
    for bad in ("a/escape.mp4", "../private/secret.mp4", "/etc/passwd"):
        with pytest.raises(OutsideRoot):
            resolve_inside(root, bad)


def test_symlinked_video_outside_the_library_is_not_cataloged(conn, media_root, outside, fake_probe):
    make_files(media_root, "Tapes/a.mpg", "Tapes/b.mpg")
    (media_root / "Tapes/escape.mp4").symlink_to(outside / "secret.mp4")
    (media_root / "Tapes/alias.mpg").symlink_to(media_root / "Tapes/a.mpg")
    lib = create_library(conn, media_root, "Media", str(media_root))
    result = scan_library(conn, lib, probe_fn=fake_probe)
    paths = {r[0] for r in conn.execute("SELECT rel_path FROM media_items")}
    assert paths == {"Tapes/a.mpg", "Tapes/b.mpg", "Tapes/alias.mpg"}
    assert result["outside_library"] == 1
    assert all("private" not in str(p) for p in fake_probe.calls)  # never even probed


def test_symlinked_folder_outside_the_library_is_not_walked(conn, media_root, outside, fake_probe):
    make_files(media_root, "Tapes/a.mpg", "Tapes/b.mpg")
    (media_root / "Linked").symlink_to(outside, target_is_directory=True)
    lib = create_library(conn, media_root, "Media", str(media_root))
    scan_library(conn, lib, probe_fn=fake_probe)
    assert {r[0] for r in conn.execute("SELECT rel_path FROM media_items")} == {"Tapes/a.mpg", "Tapes/b.mpg"}


def test_pictures_outside_the_library_are_ignored(conn, media_root, outside, fake_probe):
    make_files(media_root, "Tapes/a.mpg", "Tapes/b.mpg")
    (media_root / "Tapes/a.png").symlink_to(outside / "secret.png")
    (media_root / "Tapes/folder.png").symlink_to(outside / "folder.png")
    lib = create_library(conn, media_root, "Media", str(media_root))
    scan_library(conn, lib, probe_fn=fake_probe)
    assert conn.execute("SELECT poster_path FROM media_items WHERE rel_path = 'Tapes/a.mpg'").fetchone()[0] is None
    assert conn.execute("SELECT COUNT(*) FROM folder_art").fetchone()[0] == 0


@requires_ffmpeg
def test_file_swapped_for_a_symlink_after_scanning_is_not_served(client, media_root, outside, clips):
    """Serving re-checks: a cataloged file later replaced by a symlink out of the library."""
    folder = media_root / "Clips"
    folder.mkdir()
    (folder / "a.mp4").write_bytes((clips / "h264_aac.mp4").read_bytes())
    (folder / "b.mkv").write_bytes((clips / "h264_aac.mkv").read_bytes())
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=320x180", "-frames:v", "1",
                    str(folder / "a.png")], check=True)
    lib = client.post("/api/libraries", json={"name": "Clips", "path": str(folder)}).json()["id"]
    client.post(f"/api/libraries/{lib}/scan")
    client.scans.wait_idle()
    ids = {i["title"]: i["id"] for i in client.get(f"/api/libraries/{lib}/browse").json()["items"]}

    (outside / "secret.png").write_bytes((folder / "a.png").read_bytes())
    for name, target in (("a.mp4", "secret.mp4"), ("b.mkv", "secret.mp4"), ("a.png", "secret.png")):
        (folder / name).unlink()
        (folder / name).symlink_to(outside / target)

    assert client.get(f"/api/items/{ids['a']}/file").status_code == 404
    assert client.get(f"/api/items/{ids['b']}/stream").status_code == 404
    assert client.get(f"/api/items/{ids['a']}/thumb").status_code == 404
