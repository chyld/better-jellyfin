"""Scanning a folder tree laid out like the real NAS, with ffprobe faked out."""
import os
import shutil

import pytest

from reel.libraries import create_library
from reel.plan import plan
from reel.scanner import ScanError, scan_library

from conftest import make_files

LAYOUT = [
    # Personal/Camcorder: flat folder, image next to each video, folder art
    "Personal/Camcorder/clip01.avi", "Personal/Camcorder/clip01.png",
    "Personal/Camcorder/clip02.avi", "Personal/Camcorder/clip02.png",
    "Personal/Camcorder/tape01.wmv", "Personal/Camcorder/tape01.png",
    "Personal/Camcorder/folder.png",
    # Personal/Tapes: year-prefixed names, mixed formats, one video without a poster
    "Personal/Tapes/1992.zoo-trip.mpg", "Personal/Tapes/1992.zoo-trip.png",
    "Personal/Tapes/lecture.asf", "Personal/Tapes/lecture.png",
    "Personal/Tapes/interview.mov", "Personal/Tapes/interview.png",
    "Personal/Tapes/1996.beach-1.mpg",
    # Personal/Lectures: many videos, no posters, just folder.jpg
    "Personal/Lectures/session14-01-2022.mp4", "Personal/Lectures/session19-01-2022.mp4", "Personal/Lectures/folder.jpg",
    # Films: one folder per video
    "Films/Collection/Classics/0360/movie.mp4", "Films/Collection/Classics/0360/movie.png",
    "Films/Collection/Classics/0305/movie.mp4", "Films/Collection/Classics/0305/movie.jpg",
    "Films/Collection/Classics/0370/movie.mkv", "Films/Collection/Classics/0370/movie.png",
    "Films/Collection/Drama/0902/rough-cut.mp4",
    # Things that aren't videos, or should be skipped
    "Personal/notes.txt",
    "Personal/.hidden/secret.mp4",
    "Personal/Camcorder/.DS_Store",
]


@pytest.fixture
def library(conn, media_root):
    make_files(media_root, *LAYOUT)
    return create_library(conn, media_root, "Media", str(media_root))


def items(conn, library_id):
    rows = conn.execute("SELECT * FROM media_items WHERE library_id = ?", (library_id,))
    return {r["rel_path"]: dict(r) for r in rows}


def test_scan_finds_every_video(conn, library, fake_probe):
    result = scan_library(conn, library, probe_fn=fake_probe)
    found = items(conn, library)
    assert set(found) == {
        "Personal/Camcorder/clip01.avi", "Personal/Camcorder/clip02.avi", "Personal/Camcorder/tape01.wmv",
        "Personal/Tapes/1992.zoo-trip.mpg", "Personal/Tapes/lecture.asf",
        "Personal/Tapes/interview.mov", "Personal/Tapes/1996.beach-1.mpg",
        "Personal/Lectures/session14-01-2022.mp4", "Personal/Lectures/session19-01-2022.mp4",
        "Films/Collection/Classics/0360/movie.mp4", "Films/Collection/Classics/0305/movie.mp4",
        "Films/Collection/Classics/0370/movie.mkv", "Films/Collection/Drama/0902/rough-cut.mp4",
    }
    assert result == {"total": 13, "added": 13, "updated": 0, "moved": 0, "unchanged": 0, "failed": 0, "missing": 0, "removed": 0, "unreadable_folders": [], "unreadable_files": 0, "outside_library": 0}


def test_scan_titles_and_posters(conn, library, fake_probe):
    scan_library(conn, library, probe_fn=fake_probe)
    found = items(conn, library)

    def check(path, title, year, poster):
        row = found[path]
        assert (row["title"], row["year"], row["poster_path"]) == (title, year, poster)

    check("Personal/Camcorder/clip01.avi", "clip01", None, "Personal/Camcorder/clip01.png")
    check("Personal/Tapes/1992.zoo-trip.mpg", "zoo-trip", 1992, "Personal/Tapes/1992.zoo-trip.png")
    check("Personal/Tapes/1996.beach-1.mpg", "beach-1", 1996, None)
    # folder.jpg is folder art, not a poster, when the folder holds many videos.
    check("Personal/Lectures/session14-01-2022.mp4", "session14-01-2022", None, None)
    check("Films/Collection/Classics/0360/movie.mp4", "0360", None, "Films/Collection/Classics/0360/movie.png")
    check("Films/Collection/Classics/0305/movie.mp4", "0305", None, "Films/Collection/Classics/0305/movie.jpg")
    check("Films/Collection/Drama/0902/rough-cut.mp4", "0902", None, None)


def test_scan_records_folder_art(conn, library, fake_probe):
    scan_library(conn, library, probe_fn=fake_probe)
    art = dict(conn.execute("SELECT rel_dir, art_path FROM folder_art WHERE library_id = ?", (library,)).fetchall())
    assert art == {"Personal/Camcorder": "Personal/Camcorder/folder.png", "Personal/Lectures": "Personal/Lectures/folder.jpg"}


def test_scan_stores_probe_details_and_play_mode(conn, library, fake_probe):
    scan_library(conn, library, probe_fn=fake_probe)
    found = items(conn, library)
    modes = {path: plan(row).mode for path, row in found.items()}
    assert modes["Personal/Lectures/session14-01-2022.mp4"] == "direct"
    assert modes["Personal/Tapes/interview.mov"] == "direct"
    assert modes["Films/Collection/Classics/0370/movie.mkv"] == "remux"
    assert modes["Personal/Camcorder/clip01.avi"] == "transcode"
    assert modes["Personal/Camcorder/tape01.wmv"] == "transcode"
    assert modes["Personal/Tapes/1992.zoo-trip.mpg"] == "transcode"
    trip = found["Personal/Tapes/1992.zoo-trip.mpg"]
    assert (trip["video_codec"], trip["audio_codec"], trip["interlaced"]) == ("mpeg2video", "mp2", 1)
    assert (trip["width"], trip["height"], trip["duration"]) == (720, 480, 60.0)


def test_rescan_without_changes_probes_nothing(conn, library, fake_probe):
    scan_library(conn, library, probe_fn=fake_probe)
    fake_probe.calls.clear()
    result = scan_library(conn, library, probe_fn=fake_probe)
    assert fake_probe.calls == []
    assert result == {"total": 13, "added": 0, "updated": 0, "moved": 0, "unchanged": 13, "failed": 0, "missing": 0, "removed": 0, "unreadable_folders": [], "unreadable_files": 0, "outside_library": 0}


def test_rescan_picks_up_added_changed_and_deleted_files(conn, library, fake_probe, media_root):
    scan_library(conn, library, probe_fn=fake_probe)
    fake_probe.calls.clear()

    make_files(media_root, "Personal/Tapes/1998.hiking.mpg")
    (media_root / "Personal/Camcorder/clip01.avi").write_bytes(b"re-encoded")
    (media_root / "Personal/Camcorder/clip02.avi").unlink()
    shutil.rmtree(media_root / "Films/Collection/Drama/0902")

    result = scan_library(conn, library, probe_fn=fake_probe)
    assert sorted(p.name for p in fake_probe.calls) == ["1998.hiking.mpg", "clip01.avi"]
    assert result == {
        "total": 12, "added": 1, "updated": 1, "moved": 0, "unchanged": 10, "failed": 0,
        "missing": 2, "removed": 0, "unreadable_folders": [], "unreadable_files": 0,
        "outside_library": 0,
    }
    found = items(conn, library)
    # Deleted files are marked missing first; they're removed after a grace period.
    assert found["Personal/Camcorder/clip02.avi"]["missing_since"] is not None
    assert found["Personal/Camcorder/clip01.avi"]["size"] == len(b"re-encoded")
    assert found["Personal/Tapes/1998.hiking.mpg"]["year"] == 1998


def test_touching_a_file_triggers_reprobe(conn, library, fake_probe, media_root):
    scan_library(conn, library, probe_fn=fake_probe)
    fake_probe.calls.clear()
    path = media_root / "Personal/Tapes/interview.mov"
    os.utime(path, (1_000_000_000, 1_000_000_000))
    scan_library(conn, library, probe_fn=fake_probe)
    assert fake_probe.calls == [path]


def test_folder_image_is_a_folder_preview_not_a_video_preview(conn, media_root, fake_probe):
    make_files(media_root, "Drama/0902/rough-cut.mp4", "Drama/0902/folder.jpg", "Drama/0903/zombie.mp4", "Drama/0903/zombie.png")
    lib = create_library(conn, media_root, "All", str(media_root))
    scan_library(conn, lib, probe_fn=fake_probe)
    posters = {r["rel_path"]: r["poster_path"] for r in items(conn, lib).values()}
    assert posters == {"Drama/0902/rough-cut.mp4": None, "Drama/0903/zombie.mp4": "Drama/0903/zombie.png"}
    art = dict(conn.execute("SELECT rel_dir, art_path FROM folder_art WHERE library_id = ?", (lib,)).fetchall())
    assert art == {"Drama/0902": "Drama/0902/folder.jpg"}


def test_lone_video_beside_folders_keeps_its_name(conn, media_root, fake_probe):
    make_files(media_root, "Movies/trailer.mp4", "Movies/Avatar/avatar.mkv")
    lib = create_library(conn, media_root, "All", str(media_root))
    scan_library(conn, lib, probe_fn=fake_probe)
    assert {r["rel_path"]: r["title"] for r in items(conn, lib).values()} == {
        "Movies/trailer.mp4": "trailer",
        "Movies/Avatar/avatar.mkv": "Avatar",
    }


def test_rescan_updates_titles_without_reprobing(conn, library, fake_probe, media_root):
    """Title rules can change between versions; a rescan applies them to old rows."""
    scan_library(conn, library, probe_fn=fake_probe)
    conn.execute("UPDATE media_items SET title = 'rough-cut' WHERE rel_path = 'Films/Collection/Drama/0902/rough-cut.mp4'")
    fake_probe.calls.clear()
    scan_library(conn, library, probe_fn=fake_probe)
    assert fake_probe.calls == []
    assert items(conn, library)["Films/Collection/Drama/0902/rough-cut.mp4"]["title"] == "0902"


def test_rescan_picks_up_new_poster_without_reprobing(conn, library, fake_probe, media_root):
    scan_library(conn, library, probe_fn=fake_probe)
    fake_probe.calls.clear()
    make_files(media_root, "Films/Collection/Drama/0902/rough-cut.jpg")
    scan_library(conn, library, probe_fn=fake_probe)
    assert fake_probe.calls == []
    assert items(conn, library)["Films/Collection/Drama/0902/rough-cut.mp4"]["poster_path"] == "Films/Collection/Drama/0902/rough-cut.jpg"


def test_unreadable_file_is_kept_and_retried_next_scan(conn, library, fake_probe):
    fake_probe.fail.add("clip01.avi")
    result = scan_library(conn, library, probe_fn=fake_probe)
    assert result["failed"] == 1
    row = items(conn, library)["Personal/Camcorder/clip01.avi"]
    assert plan(row).mode == "unsupported"
    assert "Invalid data" in row["probe_error"]

    # Once the file reads fine, the next scan fixes it up.
    fake_probe.fail.clear()
    fake_probe.calls.clear()
    result = scan_library(conn, library, probe_fn=fake_probe)
    assert [p.name for p in fake_probe.calls] == ["clip01.avi"]
    row = items(conn, library)["Personal/Camcorder/clip01.avi"]
    assert (plan(row).mode, row["probe_error"]) == ("transcode", None)
    assert result["failed"] == 0


def test_missing_library_folder_fails_without_deleting_catalog(conn, library, fake_probe, media_root, tmp_path):
    scan_library(conn, library, probe_fn=fake_probe)
    # Simulate the NAS going offline.
    media_root.rename(tmp_path / "offline")
    with pytest.raises(ScanError, match="missing"):
        scan_library(conn, library, probe_fn=fake_probe)
    assert len(items(conn, library)) == 13


def test_empty_library(conn, media_root, fake_probe):
    (media_root / "empty").mkdir()
    lib = create_library(conn, media_root, "Empty", str(media_root / "empty"))
    assert scan_library(conn, lib, probe_fn=fake_probe)["total"] == 0


def test_progress_is_reported(conn, library, fake_probe):
    updates = []
    scan_library(conn, library, probe_fn=fake_probe, on_progress=lambda d, t: updates.append((d, t)))
    assert updates[0] == (0, 13)
    assert updates[-1] == (13, 13)
    assert [d for d, _ in updates] == sorted(d for d, _ in updates)


def test_libraries_are_scanned_separately(conn, media_root, fake_probe):
    make_files(media_root, *LAYOUT)
    internal = create_library(conn, media_root, "Personal", str(media_root / "Personal"))
    external = create_library(conn, media_root, "Films", str(media_root / "Films"))
    scan_library(conn, internal, probe_fn=fake_probe)
    scan_library(conn, external, probe_fn=fake_probe)
    assert len(items(conn, internal)) == 9
    assert len(items(conn, external)) == 4
    # Paths are relative to each library's own folder.
    assert "Camcorder/clip01.avi" in items(conn, internal)
    assert items(conn, external)["Collection/Classics/0360/movie.mp4"]["title"] == "0360"


# ---- Doing only the work that's needed --------------------------------------------------


def _count_video_updates(conn):
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS writes (n INTEGER)")
    conn.execute("DELETE FROM temp.writes")
    conn.execute("CREATE TEMP TRIGGER IF NOT EXISTS count_writes AFTER UPDATE ON media_items "
                 "BEGIN INSERT INTO temp.writes VALUES (1); END")
    conn.commit()
    return lambda: conn.execute("SELECT COUNT(*) FROM temp.writes").fetchone()[0]


def test_a_rescan_of_unchanged_files_writes_no_video_rows(conn, media_root, fake_probe):
    make_files(media_root, "Tapes/a.mpg", "Tapes/b.mpg", "Tapes/c.mpg")
    lib = create_library(conn, media_root, "Media", str(media_root))
    scan_library(conn, lib, probe_fn=fake_probe)
    writes = _count_video_updates(conn)
    scan_library(conn, lib, probe_fn=fake_probe)
    assert writes() == 0
    make_files(media_root, "Tapes/b.png")                 # a poster appears for b
    scan_library(conn, lib, probe_fn=fake_probe)
    assert writes() == 1
    row = conn.execute("SELECT poster_path FROM media_items WHERE rel_path = 'Tapes/b.mpg'").fetchone()
    assert row["poster_path"] == "Tapes/b.png"


def test_a_video_that_comes_back_is_shown_again_without_a_reprobe(conn, media_root, fake_probe):
    import os
    make_files(media_root, "Tapes/a.mpg", "Tapes/b.mpg")
    lib = create_library(conn, media_root, "Media", str(media_root))
    scan_library(conn, lib, probe_fn=fake_probe)
    os.rename(media_root / "Tapes/a.mpg", media_root / "a.away")
    scan_library(conn, lib, probe_fn=fake_probe)
    assert conn.execute("SELECT missing_since FROM media_items WHERE rel_path = 'Tapes/a.mpg'").fetchone()[0]
    os.rename(media_root / "a.away", media_root / "Tapes/a.mpg")
    calls = len(fake_probe.calls)
    scan_library(conn, lib, probe_fn=fake_probe)
    assert conn.execute("SELECT missing_since FROM media_items WHERE rel_path = 'Tapes/a.mpg'").fetchone()[0] is None
    assert len(fake_probe.calls) == calls                # same size and time: not probed again


def test_one_slow_probe_doesnt_hold_back_the_others(conn, media_root, fake_probe):
    import threading
    import time

    make_files(media_root, *[f"Tapes/v{i}.mpg" for i in range(6)])
    lib = create_library(conn, media_root, "Media", str(media_root))
    release = threading.Event()

    def probe(path):
        if path.name == "v0.mpg":
            release.wait(5)                                # a slow file, first in line
        return fake_probe(path)

    seen = []

    def progress(done, total):
        seen.append(done)
        if done == total - 1:
            release.set()                                  # all the others are recorded first

    began = time.monotonic()
    scan_library(conn, lib, probe_fn=probe, workers=2, on_progress=progress)
    assert time.monotonic() - began < 4                    # the slow one wasn't waited on first
    assert seen[-1] == 6


def test_the_walk_only_checks_videos_and_pictures(conn, media_root, fake_probe, monkeypatch):
    """Sidecar files (.nfo, .srt, ...) cost nothing: each check is a round trip on SMB."""
    import os
    make_files(media_root, "Tapes/a.mpg", "Tapes/a.png", *[f"Tapes/extra{i}.nfo" for i in range(40)],
               *[f"Tapes/extra{i}.srt" for i in range(40)])
    lib = create_library(conn, media_root, "Media", str(media_root))
    checked = []
    real = os.path.islink
    monkeypatch.setattr(os.path, "islink", lambda p: checked.append(os.path.basename(p)) or real(p))
    scan_library(conn, lib, probe_fn=fake_probe)
    files = [name for name in checked if "." in name]      # (os.walk checks the folders itself)
    assert sorted(files) == ["a.mpg", "a.png"]


def test_expired_missing_videos_are_removed_in_bulk(conn, media_root, fake_probe):
    from datetime import timedelta
    import os
    make_files(media_root, *[f"Tapes/v{i}.mpg" for i in range(600)], "Tapes/keep.mpg")
    lib = create_library(conn, media_root, "Media", str(media_root))
    scan_library(conn, lib, probe_fn=fake_probe)
    for i in range(600):
        os.remove(media_root / f"Tapes/v{i}.mpg")
    scan_library(conn, lib, probe_fn=fake_probe)          # marked missing
    result = scan_library(conn, lib, probe_fn=fake_probe, missing_grace=timedelta(0))
    assert result["removed"] == 600
    assert [r[0] for r in conn.execute("SELECT rel_path FROM media_items")] == ["Tapes/keep.mpg"]
