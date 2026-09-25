"""Scans must never throw away catalog data because of something they couldn't read."""
import os
from datetime import timedelta

import pytest

from reel.libraries import create_library
from reel.scanner import ScanError, scan_library

from conftest import make_files


def rows(conn, lib):
    return {r["rel_path"]: dict(r) for r in conn.execute("SELECT * FROM media_items WHERE library_id = ?", (lib,))}


def tag(conn, rel_path, name):
    item = conn.execute("SELECT id FROM media_items WHERE rel_path = ?", (rel_path,)).fetchone()["id"]
    tag_id = conn.execute("INSERT INTO tags (uid, name) VALUES (?, ?)", (name + "-uid", name)).lastrowid
    conn.execute("INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)", (item, tag_id))
    conn.commit()


def item_tags(conn, rel_path):
    return [r[0] for r in conn.execute(
        "SELECT t.name FROM item_tags it JOIN tags t ON t.id = it.tag_id "
        "JOIN media_items m ON m.id = it.item_id WHERE m.rel_path = ?", (rel_path,))]


@pytest.fixture
def lib(conn, media_root, fake_probe):
    make_files(media_root, "Tapes/a.mpg", "Tapes/b.mpg", "Camcorder/clip1.avi", "Camcorder/clip2.avi",
               "Camcorder/folder.png")
    lib = create_library(conn, media_root, "Media", str(media_root))
    scan_library(conn, lib, probe_fn=fake_probe)
    return lib


needs_permissions = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores folder permissions")


@needs_permissions
def test_unreadable_folder_keeps_its_videos_tags_and_art(conn, lib, media_root, fake_probe):
    tag(conn, "Tapes/a.mpg", "family")
    tapes = media_root / "Tapes"
    tapes.chmod(0o000)  # e.g. a permissions change or a flaky SMB listing
    try:
        result = scan_library(conn, lib, probe_fn=fake_probe)
    finally:
        tapes.chmod(0o755)
    found = rows(conn, lib)
    assert {"Tapes/a.mpg", "Tapes/b.mpg"} <= set(found)
    assert found["Tapes/a.mpg"]["missing_since"] is None      # not even marked missing
    assert item_tags(conn, "Tapes/a.mpg") == ["family"]
    assert result["unreadable_folders"] == ["Tapes"]
    assert result["removed"] == 0 and result["missing"] == 0
    warning = conn.execute("SELECT last_scan_warning FROM libraries WHERE id = ?", (lib,)).fetchone()[0]
    assert "Tapes" in warning


@needs_permissions
def test_unreadable_folder_keeps_folder_art(conn, lib, media_root, fake_probe):
    camcorder = media_root / "Camcorder"
    camcorder.chmod(0o000)
    try:
        scan_library(conn, lib, probe_fn=fake_probe)
    finally:
        camcorder.chmod(0o755)
    art = dict(conn.execute("SELECT rel_dir, art_path FROM folder_art WHERE library_id = ?", (lib,)).fetchall())
    assert art == {"Camcorder": "Camcorder/folder.png"}


def test_file_that_cannot_be_read_is_kept(conn, lib, media_root, fake_probe, monkeypatch):
    import reel.scanner

    real_stat = os.stat
    def flaky_stat(path, *args, **kwargs):
        if str(path).endswith("a.mpg"):
            raise PermissionError(13, "Permission denied", str(path))
        return real_stat(path, *args, **kwargs)
    monkeypatch.setattr(reel.scanner.os, "stat", flaky_stat)
    result = scan_library(conn, lib, probe_fn=fake_probe)
    assert rows(conn, lib)["Tapes/a.mpg"]["missing_since"] is None
    assert result["unreadable_files"] == 1


def test_empty_mount_changes_nothing(conn, lib, media_root, fake_probe):
    # The NAS isn't mounted: the mount point is there, but empty.
    for path in sorted(media_root.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    with pytest.raises(ScanError, match="is empty, but 4 videos"):
        scan_library(conn, lib, probe_fn=fake_probe)
    assert len(rows(conn, lib)) == 4
    assert all(r["missing_since"] is None for r in rows(conn, lib).values())


def test_deleted_file_is_marked_missing_first(conn, lib, media_root, fake_probe):
    tag(conn, "Tapes/a.mpg", "family")
    (media_root / "Tapes/a.mpg").unlink()
    result = scan_library(conn, lib, probe_fn=fake_probe)
    assert result["missing"] == 1 and result["removed"] == 0
    row = rows(conn, lib)["Tapes/a.mpg"]
    assert row["missing_since"] is not None
    assert item_tags(conn, "Tapes/a.mpg") == ["family"]  # kept during the grace period


def test_missing_file_that_comes_back_keeps_everything(conn, lib, media_root, fake_probe):
    tag(conn, "Tapes/a.mpg", "family")
    uid = rows(conn, lib)["Tapes/a.mpg"]["uid"]
    moved = media_root.parent / "a.mpg"
    (media_root / "Tapes/a.mpg").rename(moved)
    scan_library(conn, lib, probe_fn=fake_probe)
    moved.rename(media_root / "Tapes/a.mpg")  # the NAS is back
    scan_library(conn, lib, probe_fn=fake_probe)
    row = rows(conn, lib)["Tapes/a.mpg"]
    assert row["missing_since"] is None and row["uid"] == uid
    assert item_tags(conn, "Tapes/a.mpg") == ["family"]


def test_missing_file_is_removed_after_the_grace_period(conn, lib, media_root, fake_probe):
    (media_root / "Tapes/a.mpg").unlink()
    scan_library(conn, lib, probe_fn=fake_probe)
    # Still inside the grace period: kept.
    assert scan_library(conn, lib, probe_fn=fake_probe, missing_grace=timedelta(days=7))["removed"] == 0
    # Pretend it went missing 8 days ago.
    conn.execute("UPDATE media_items SET missing_since = datetime('now', '-8 days') WHERE rel_path = 'Tapes/a.mpg'")
    conn.commit()
    result = scan_library(conn, lib, probe_fn=fake_probe, missing_grace=timedelta(days=7))
    assert result["removed"] == 1
    assert "Tapes/a.mpg" not in rows(conn, lib)


def test_no_grace_period_removes_at_once(conn, lib, media_root, fake_probe):
    (media_root / "Tapes/a.mpg").unlink()
    result = scan_library(conn, lib, probe_fn=fake_probe, missing_grace=timedelta(0))
    assert result["removed"] == 1 and "Tapes/a.mpg" not in rows(conn, lib)


def test_whole_folder_deleted_is_marked_missing(conn, lib, media_root, fake_probe):
    """A folder that's really gone (its parent listed fine) is missing, not 'unreadable'."""
    for name in ("a.mpg", "b.mpg"):
        (media_root / "Tapes" / name).unlink()
    (media_root / "Tapes").rmdir()
    result = scan_library(conn, lib, probe_fn=fake_probe)
    assert result["missing"] == 2 and result["unreadable_folders"] == []


# ---- Missing videos are hidden, then come back ---------------------------------------


def test_missing_videos_are_hidden_but_come_back(client, media_root):
    make_files(media_root, "Tapes/a.mpg", "Tapes/b.mpg")
    lib = client.post("/api/libraries", json={"name": "Tapes", "path": str(media_root / "Tapes")}).json()["id"]

    def rescan():
        client.post(f"/api/libraries/{lib}/scan")
        client.scans.wait_idle()

    rescan()
    a = next(i for i in client.get(f"/api/libraries/{lib}/browse").json()["items"] if i["title"] == "a")
    family = client.post(f"/api/items/{a['id']}/tags", json={"name": "family"}).json()[0]

    moved = media_root / "a.mpg"
    (media_root / "Tapes/a.mpg").rename(moved)
    rescan()
    assert [i["title"] for i in client.get(f"/api/libraries/{lib}/browse").json()["items"]] == ["b"]
    assert client.get("/api/libraries").json()[0]["item_count"] == 1
    assert client.get(f"/api/tags/{family['id']}").json()["items"] == []
    assert client.get("/api/tags").json() == []           # a tag with only missing videos is hidden
    detail = client.get(f"/api/items/{a['id']}").json()
    assert detail["missing"] is True and [t["name"] for t in detail["tags"]] == ["family"]

    moved.rename(media_root / "Tapes/a.mpg")               # back again
    rescan()
    assert client.get(f"/api/items/{a['id']}").json()["missing"] is False
    assert [i["title"] for i in client.get(f"/api/tags/{family['id']}").json()["items"]] == ["a"]


@needs_permissions
def test_scan_warning_shows_on_the_library(client, media_root):
    make_files(media_root, "Lib/Tapes/a.mpg", "Lib/Camcorder/b.avi")
    lib = client.post("/api/libraries", json={"name": "Lib", "path": str(media_root / "Lib")}).json()["id"]
    client.post(f"/api/libraries/{lib}/scan")
    client.scans.wait_idle()
    (media_root / "Lib/Tapes").chmod(0o000)
    try:
        client.post(f"/api/libraries/{lib}/scan")
        client.scans.wait_idle()
    finally:
        (media_root / "Lib/Tapes").chmod(0o755)
    listed = client.get("/api/libraries").json()[0]
    assert "couldn't read 1 folder(s): Tapes" in listed["last_scan_warning"]
    assert listed["item_count"] == 2
    assert listed["scan"]["result"]["unreadable_folders"] == ["Tapes"]


def test_stopped_scan_marks_nothing_missing(conn, lib, media_root, fake_probe):
    """A scan stopped halfway (Reel shutting down) must not treat unwalked or
    unprobed videos as gone."""
    import threading

    from reel.scanner import ScanCancelled

    os.remove(media_root / "Tapes/a.mpg")                       # really gone...
    make_files(media_root, *[f"New/n{i}.mpg" for i in range(6)])
    before = conn.execute("SELECT last_scan_at FROM libraries WHERE id = ?", (lib,)).fetchone()[0]
    cancel = threading.Event()

    def probe_then_stop(path):
        if len(fake_probe.calls) >= 2:
            cancel.set()
        return fake_probe(path)

    with pytest.raises(ScanCancelled):
        scan_library(conn, lib, probe_fn=probe_then_stop, workers=1, cancel=cancel)
    after = rows(conn, lib)
    assert after["Tapes/a.mpg"]["missing_since"] is None        # ...but this scan didn't finish
    new = [r for p, r in after.items() if p.startswith("New/")]
    assert len(new) < 6 and all(r["video_codec"] for r in new)  # what was recorded is complete
    assert conn.execute("SELECT last_scan_at FROM libraries WHERE id = ?", (lib,)).fetchone()[0] == before


def test_scan_stopped_before_it_starts_changes_nothing(conn, lib, media_root, fake_probe):
    import threading

    from reel.scanner import ScanCancelled

    make_files(media_root, "New/n.mpg")
    before = rows(conn, lib)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ScanCancelled):
        scan_library(conn, lib, probe_fn=fake_probe, cancel=cancel)
    assert rows(conn, lib) == before


def test_stopped_while_matching_moves_changes_nothing(conn, media_root, fake_probe, monkeypatch):
    """Stopped while fingerprinting new files to spot moved ones: no move is half-applied."""
    import threading

    from reel import scanner
    from reel.scanner import ScanCancelled

    for i in range(4):
        (media_root / "Tapes").mkdir(exist_ok=True)
        (media_root / f"Tapes/t{i}.mpg").write_bytes(f"tape {i} ".encode() * 20000)
    lib = create_library(conn, media_root, "Media", str(media_root))
    scan_library(conn, lib, probe_fn=fake_probe)
    (media_root / "Moved").mkdir()
    for i in range(4):
        os.rename(media_root / f"Tapes/t{i}.mpg", media_root / f"Moved/t{i}.mpg")
    before = rows(conn, lib)
    cancel = threading.Event()
    real = scanner.fingerprint

    def fingerprint_then_stop(path, size):
        cancel.set()                                   # Reel is asked to stop mid-way
        return real(path, size)

    monkeypatch.setattr(scanner, "fingerprint", fingerprint_then_stop)
    with pytest.raises(ScanCancelled):
        scan_library(conn, lib, probe_fn=fake_probe, workers=1, cancel=cancel)
    assert rows(conn, lib) == before                   # nothing moved, nothing marked missing
