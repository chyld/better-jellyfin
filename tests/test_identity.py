"""A video keeps its identity (id, tags, pictures) when its file is moved or renamed."""
import os
import sqlite3

import pytest

from reel.db import connect, init_db
from reel.libraries import create_library
from reel.probe import PROBE_VERSION
from reel.scanner import fingerprint, scan_library


def write(root, rel, content: bytes):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def video(n: int) -> bytes:
    """Distinct contents per video (real files differ; empty placeholders wouldn't)."""
    return (f"video {n} ".encode() * 30000)[: 150_000 + n]


@pytest.fixture
def lib(conn, media_root, fake_probe):
    write(media_root, "Tapes/a.mpg", video(1))
    write(media_root, "Tapes/b.mpg", video(2))
    write(media_root, "Camcorder/c.avi", video(3))
    lib = create_library(conn, media_root, "Media", str(media_root))
    scan_library(conn, lib, probe_fn=fake_probe)
    return lib


def row(conn, rel_path):
    r = conn.execute("SELECT * FROM media_items WHERE rel_path = ?", (rel_path,)).fetchone()
    return dict(r) if r else None


def add_tag(conn, rel_path, name):
    item = row(conn, rel_path)["id"]
    tag = conn.execute("INSERT INTO tags (uid, name) VALUES (?, ?)", (name, name)).lastrowid
    conn.execute("INSERT INTO item_tags (item_id, tag_id) VALUES (?, ?)", (item, tag))
    conn.commit()


def tags_of(conn, rel_path):
    return [r[0] for r in conn.execute(
        "SELECT t.name FROM tags t JOIN item_tags it ON it.tag_id = t.id JOIN media_items m ON m.id = it.item_id "
        "WHERE m.rel_path = ?", (rel_path,))]


# ---- Fingerprints ----------------------------------------------------------------------


def test_fingerprint(tmp_path):
    a = write(tmp_path, "a", video(1))
    same = write(tmp_path, "copy", video(1))
    other = write(tmp_path, "b", video(2))
    fp = fingerprint(a, a.stat().st_size)
    assert fp == fingerprint(same, same.stat().st_size)          # same contents, any name
    assert fp != fingerprint(other, other.stat().st_size)
    a.write_bytes(video(1)[:-1] + b"X")                            # a change near the end
    assert fingerprint(a, a.stat().st_size) != fp
    assert fingerprint(tmp_path / "missing", 10) is None


def test_scan_records_fingerprint_and_probe_version(conn, lib):
    r = row(conn, "Tapes/a.mpg")
    assert r["fingerprint"] and r["probe_version"] == PROBE_VERSION


# ---- Moves and renames ------------------------------------------------------------------


def test_renamed_file_keeps_its_identity(conn, lib, media_root, fake_probe):
    before = row(conn, "Tapes/a.mpg")
    add_tag(conn, "Tapes/a.mpg", "family")
    conn.execute("UPDATE media_items SET custom_image = 'v1' WHERE id = ?", (before["id"],))
    conn.commit()
    os.rename(media_root / "Tapes/a.mpg", media_root / "Tapes/1994.picnic.mpg")
    fake_probe.calls.clear()

    result = scan_library(conn, lib, probe_fn=fake_probe)
    after = row(conn, "Tapes/1994.picnic.mpg")
    assert after["uid"] == before["uid"] and after["id"] == before["id"]
    assert (after["title"], after["year"]) == ("picnic", 1994)   # the new name's title
    assert after["custom_image"] == "v1"
    assert tags_of(conn, "Tapes/1994.picnic.mpg") == ["family"]
    assert row(conn, "Tapes/a.mpg") is None
    assert (result["moved"], result["added"], result["missing"]) == (1, 0, 0)
    assert fake_probe.calls == []                                  # same contents: no re-probe


def test_file_moved_to_another_folder(conn, lib, media_root, fake_probe):
    uid = row(conn, "Tapes/a.mpg")["uid"]
    (media_root / "Archive").mkdir()
    os.rename(media_root / "Tapes/a.mpg", media_root / "Archive/a.mpg")
    assert scan_library(conn, lib, probe_fn=fake_probe)["moved"] == 1
    assert row(conn, "Archive/a.mpg")["uid"] == uid


def test_file_that_went_missing_then_turned_up_elsewhere(conn, lib, media_root, fake_probe):
    uid = row(conn, "Tapes/a.mpg")["uid"]
    held = media_root.parent / "held.mpg"
    os.rename(media_root / "Tapes/a.mpg", held)
    scan_library(conn, lib, probe_fn=fake_probe)                   # now missing
    assert row(conn, "Tapes/a.mpg")["missing_since"]
    os.rename(held, media_root / "Camcorder/a-copy.mpg")           # back, somewhere else
    result = scan_library(conn, lib, probe_fn=fake_probe)
    moved = row(conn, "Camcorder/a-copy.mpg")
    assert moved["uid"] == uid and moved["missing_since"] is None and result["moved"] == 1


def test_a_copy_is_a_new_video(conn, lib, media_root, fake_probe):
    """The original is still there, so the copy isn't a move."""
    write(media_root, "Archive/a.mpg", video(1))
    result = scan_library(conn, lib, probe_fn=fake_probe)
    assert (result["added"], result["moved"]) == (1, 0)
    assert row(conn, "Archive/a.mpg")["uid"] != row(conn, "Tapes/a.mpg")["uid"]


def test_ambiguous_matches_are_not_guessed(conn, media_root, fake_probe):
    write(media_root, "One/x.mpg", video(7))
    write(media_root, "Two/x.mpg", video(7))                        # identical copies
    write(media_root, "Other/y.mpg", video(8))
    lib = create_library(conn, media_root, "Media", str(media_root))
    scan_library(conn, lib, probe_fn=fake_probe)
    os.remove(media_root / "One/x.mpg")
    os.remove(media_root / "Two/x.mpg")
    write(media_root, "Three/x.mpg", video(7))                      # which one was it?
    result = scan_library(conn, lib, probe_fn=fake_probe)
    assert (result["moved"], result["added"], result["missing"]) == (0, 1, 2)


def test_changed_contents_are_not_a_move(conn, lib, media_root, fake_probe):
    os.remove(media_root / "Tapes/a.mpg")
    write(media_root, "Tapes/a-new.mpg", video(99))
    result = scan_library(conn, lib, probe_fn=fake_probe)
    assert (result["moved"], result["added"], result["missing"]) == (0, 1, 1)


def test_different_extension_is_not_a_move(conn, lib, media_root, fake_probe):
    os.rename(media_root / "Tapes/a.mpg", media_root / "Tapes/a.mp4")
    assert scan_library(conn, lib, probe_fn=fake_probe)["moved"] == 0


# ---- Folder renames carry uploaded folder pictures -----------------------------------------


def folder_image(conn, lib, rel_dir):
    return conn.execute("SELECT uid FROM folder_images WHERE library_id = ? AND rel_dir = ?", (lib, rel_dir)).fetchone()


def set_folder_image(conn, lib, rel_dir, uid):
    conn.execute("INSERT INTO folder_images (uid, library_id, rel_dir, version) VALUES (?, ?, ?, 'v1')",
                 (uid, lib, rel_dir))
    conn.commit()


def test_renamed_folder_keeps_its_uploaded_picture(conn, lib, media_root, fake_probe):
    set_folder_image(conn, lib, "Tapes", "img-1")
    os.rename(media_root / "Tapes", media_root / "Home Tapes")
    result = scan_library(conn, lib, probe_fn=fake_probe)
    assert result["moved"] == 2
    assert folder_image(conn, lib, "Home Tapes")["uid"] == "img-1"
    assert folder_image(conn, lib, "Tapes") is None


def test_renamed_folder_picture_survives_a_scan_stopped_after_the_moves(conn, lib, media_root, fake_probe):
    """Stopped right after the moves are saved: the picture has moved with them, and
    the next (complete) scan keeps it, though it no longer sees any move."""
    import threading

    from reel import custom_images
    from reel.scanner import ScanCancelled

    set_folder_image(conn, lib, "Tapes", "img-1")
    os.rename(media_root / "Tapes", media_root / "Home Tapes")
    write(media_root, "New/n.mpg", video(9))                # something to probe after the moves
    cancel = threading.Event()
    with pytest.raises(ScanCancelled):
        scan_library(conn, lib, probe_fn=fake_probe, cancel=cancel,
                     on_progress=lambda done, total: cancel.set())   # first progress: after the moves
    assert row(conn, "Home Tapes/a.mpg") is not None                # the moves were saved...
    assert folder_image(conn, lib, "Home Tapes")["uid"] == "img-1"  # ...and the picture with them

    assert scan_library(conn, lib, probe_fn=fake_probe)["moved"] == 0
    custom_images.prune(conn, media_root.parent / "images")
    assert folder_image(conn, lib, "Home Tapes")["uid"] == "img-1"


def test_nested_folders_follow_a_rename(conn, media_root, fake_probe):
    write(media_root, "Tapes/1990s/a.mpg", video(1))
    write(media_root, "Tapes/2000s/b.mpg", video(2))
    lib = create_library(conn, media_root, "Media", str(media_root))
    scan_library(conn, lib, probe_fn=fake_probe)
    set_folder_image(conn, lib, "Tapes", "top")
    set_folder_image(conn, lib, "Tapes/1990s", "nineties")
    os.rename(media_root / "Tapes", media_root / "VHS")
    scan_library(conn, lib, probe_fn=fake_probe)
    assert folder_image(conn, lib, "VHS")["uid"] == "top"
    assert folder_image(conn, lib, "VHS/1990s")["uid"] == "nineties"


# ---- Probe versions and backfilling -----------------------------------------------------------


def test_outdated_probe_version_is_reprobed_once(conn, lib, fake_probe):
    conn.execute("UPDATE media_items SET probe_version = 0 WHERE rel_path = 'Tapes/a.mpg'")
    conn.commit()
    fake_probe.calls.clear()
    scan_library(conn, lib, probe_fn=fake_probe)
    assert [p.name for p in fake_probe.calls] == ["a.mpg"]
    assert row(conn, "Tapes/a.mpg")["probe_version"] == PROBE_VERSION
    fake_probe.calls.clear()
    scan_library(conn, lib, probe_fn=fake_probe)
    assert fake_probe.calls == []


def test_rows_without_a_fingerprint_get_one_without_reprobing(conn, lib, fake_probe):
    conn.execute("UPDATE media_items SET fingerprint = NULL")
    conn.commit()
    fake_probe.calls.clear()
    scan_library(conn, lib, probe_fn=fake_probe)
    assert fake_probe.calls == []
    assert all(r[0] for r in conn.execute("SELECT fingerprint FROM media_items"))


def test_upgraded_database_reprobes_and_fingerprints(tmp_path):
    """A version-1 database: rows get probe_version 0 and no fingerprint."""
    from reel.db import SCHEMA
    path = tmp_path / "v1.db"
    old = sqlite3.connect(path)
    old.executescript(SCHEMA)
    old.execute("PRAGMA user_version = 1")
    old.execute("INSERT INTO libraries (uid, name, path) VALUES ('l', 'L', '/m')")
    old.execute("INSERT INTO media_items (uid, library_id, rel_path, title, size, mtime, play_mode) "
                "VALUES ('u', 1, 'a.mpg', 'a', 1, 1, 'transcode')")
    old.commit()
    old.close()
    init_db(path)
    r = connect(path).execute("SELECT probe_version, fingerprint FROM media_items").fetchone()
    assert (r["probe_version"], r["fingerprint"]) == (0, None)
