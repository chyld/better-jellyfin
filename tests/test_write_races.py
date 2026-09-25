"""Writes that race each other, or fail halfway, leave the catalog and its files consistent."""
import sqlite3
import threading

import pytest

from reel import tags
from reel.db import connect
from reel.libraries import LibraryError, create_library
from reel.scanner import scan_library

from conftest import make_files


def all_at_once(n, work):
    """Run work(i) in n threads released together; returns results or exceptions."""
    barrier = threading.Barrier(n)
    results = [None] * n

    def run(i):
        barrier.wait()
        try:
            results[i] = work(i)
        except Exception as exc:  # noqa: BLE001 - the test inspects it
            results[i] = exc

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return results


@pytest.fixture
def videos(conn, media_root, fake_probe, settings):
    make_files(media_root, *[f"Tapes/v{i}.mpg" for i in range(8)])
    lib = create_library(conn, media_root, "Media", str(media_root))
    scan_library(conn, lib, probe_fn=fake_probe)
    return [r[0] for r in conn.execute("SELECT uid FROM media_items ORDER BY rel_path")]


def test_the_same_new_tag_added_at_once_is_created_once(settings, videos):
    for round_ in range(5):
        name = f"family-{round_}"

        def add(i):
            c = connect(settings.db_path)
            try:
                return tags.add_tag(c, videos[i], name)
            finally:
                c.close()

        results = all_at_once(len(videos), add)
        assert not [r for r in results if isinstance(r, Exception)], results
        c = connect(settings.db_path)
        assert c.execute("SELECT COUNT(*) FROM tags WHERE name = ?", (name,)).fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM item_tags it JOIN tags t ON t.id = it.tag_id WHERE t.name = ?",
                         (name,)).fetchone()[0] == len(videos)
        c.close()


def test_a_merge_that_fails_to_save_leaves_the_tag_and_its_picture(conn, settings, videos):
    images = settings.images_dir
    tags.add_tag(conn, videos[0], "vacation")
    tags.add_tag(conn, videos[1], "holiday")
    vacation = conn.execute("SELECT uid FROM tags WHERE name = 'vacation'").fetchone()[0]
    # A picture for the tag being merged away (written as an upload would be).
    conn.execute("UPDATE tags SET image_version = 'v1' WHERE uid = ?", (vacation,))
    conn.commit()
    picture = tags.image_path(images, vacation, "v1")
    picture.parent.mkdir(parents=True, exist_ok=True)
    picture.write_bytes(b"jpeg")
    # The save fails at its last step.
    conn.execute("CREATE TRIGGER no_delete BEFORE DELETE ON tags BEGIN SELECT RAISE(ABORT, 'disk full'); END")
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        tags.rename_tag(conn, vacation, "holiday", images)

    row = conn.execute("SELECT image_version FROM tags WHERE uid = ?", (vacation,)).fetchone()
    assert row["image_version"] == "v1" and picture.read_bytes() == b"jpeg"     # untouched
    holiday = conn.execute("SELECT image_version FROM tags WHERE name = 'holiday'").fetchone()
    assert holiday["image_version"] is None                                      # rolled back

    conn.execute("DROP TRIGGER no_delete")
    conn.commit()
    merged = tags.rename_tag(conn, vacation, "holiday", images)                  # and it works later
    assert merged["merged"] and merged["image"]
    assert tags.image_path(images, merged["id"], merged["image"]).read_bytes() == b"jpeg"
    assert not picture.exists()


def test_overlapping_libraries_added_at_once_only_one_wins(conn, settings, media_root):
    make_files(media_root, "Films/Old/a.mp4")
    paths = [str(media_root / "Films"), str(media_root / "Films/Old")] * 3

    def add(i):
        c = connect(settings.db_path)
        try:
            return create_library(c, media_root, f"Library {i}", paths[i])
        finally:
            c.close()

    for _ in range(5):
        c = connect(settings.db_path)
        c.execute("DELETE FROM libraries")
        c.commit()
        c.close()
        results = all_at_once(len(paths), add)
        created = [r for r in results if isinstance(r, int)]
        refused = [r for r in results if isinstance(r, LibraryError)]
        assert len(created) == 1 and len(refused) == len(paths) - 1, results


def test_write_transaction_never_saves_the_callers_unsaved_changes(conn):
    """It used to commit whatever was pending before starting, so the caller's
    later rollback couldn't undo it."""
    from reel.db import write_transaction

    conn.execute("INSERT INTO tags (uid, name) VALUES ('t1', 'outer')")
    with pytest.raises(RuntimeError, match="no unsaved changes"):
        with write_transaction(conn):
            pass
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM tags WHERE name = 'outer'").fetchone()[0] == 0

    with pytest.raises(ValueError):
        with write_transaction(conn):
            conn.execute("INSERT INTO tags (uid, name) VALUES ('t2', 'inner')")
            raise ValueError("boom")
    assert conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 0     # rolled back
