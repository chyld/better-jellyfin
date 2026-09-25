"""The Scan buttons: background scans, progress and errors."""
import shutil
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from reel.db import init_db
from reel.main import create_app
from reel.scan_manager import ScanManager
from reel.scanner import ScanCancelled

from conftest import make_files


def add(client, name, path):
    return client.post("/api/libraries", json={"name": name, "path": str(path)}).json()


def test_scan_one_library(client, media_root):
    make_files(media_root, "Tapes/1992.zoo-trip.mpg", "Tapes/1992.zoo-trip.png", "Tapes/interview.mov")
    lib = add(client, "Tapes", media_root / "Tapes")

    res = client.post(f"/api/libraries/{lib['id']}/scan")
    assert res.status_code == 202
    assert res.json()["state"] in ("queued", "scanning", "done")
    client.scans.wait_idle()

    body = client.get("/api/libraries").json()[0]
    assert body["item_count"] == 2
    assert body["last_scan_at"] is not None
    assert body["scan"]["state"] == "done"
    assert body["scan"]["done"] == body["scan"]["total"] == 2
    assert body["scan"]["result"]["added"] == 2


def test_scan_all(client, media_root):
    make_files(media_root, "Tapes/a.mpg", "Lectures/b.mp4", "Lectures/c.mp4")
    add(client, "Tapes", media_root / "Tapes")
    add(client, "Lectures", media_root / "Lectures")
    assert client.post("/api/libraries/scan").status_code == 202
    client.scans.wait_idle()
    counts = {lib["name"]: lib["item_count"] for lib in client.get("/api/libraries").json()}
    assert counts == {"Tapes": 1, "Lectures": 2}


def test_scan_unknown_library(client):
    assert client.post("/api/libraries/42/scan").status_code == 404


def test_scan_failure_is_reported(client, media_root):
    make_files(media_root, "Tapes/a.mpg")
    lib = add(client, "Tapes", media_root / "Tapes")
    shutil.rmtree(media_root / "Tapes")  # e.g. the NAS share went away

    client.post(f"/api/libraries/{lib['id']}/scan")
    client.scans.wait_idle()
    body = client.get("/api/libraries").json()[0]
    assert body["scan"]["state"] == "error"
    assert "missing" in body["scan"]["error"]
    assert "missing" in body["last_scan_error"]


def test_successful_scan_clears_previous_error(client, media_root):
    lib = add(client, "Tapes", make_dir(media_root / "Tapes"))
    shutil.rmtree(media_root / "Tapes")
    client.post(f"/api/libraries/{lib['id']}/scan")
    client.scans.wait_idle()

    make_files(media_root, "Tapes/a.mpg")
    client.post(f"/api/libraries/{lib['id']}/scan")
    client.scans.wait_idle()
    body = client.get("/api/libraries").json()[0]
    assert body["last_scan_error"] is None
    assert body["scan"]["state"] == "done"


def make_dir(path):
    path.mkdir(parents=True)
    return path


class BlockingScan:
    """A scan that reports some progress, then waits until released."""

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.runs = 0

    def __call__(self, conn, library_id, *, workers, on_progress, cancel=None):
        self.runs += 1
        on_progress(3, 10)
        self.started.set()
        # Waits like a long scan, stopping when asked (as scan_library does).
        deadline = time.monotonic() + 10
        while not self.release.is_set():
            if cancel is not None and cancel.is_set():
                raise ScanCancelled("The scan was stopped.")
            assert time.monotonic() < deadline
            time.sleep(0.01)
        on_progress(10, 10)
        return {"total": 10, "added": 10, "updated": 0, "removed": 0, "unchanged": 0, "failed": 0}


def blocking_client(settings, scan):
    init_db(settings.db_path)
    return TestClient(create_app(settings, ScanManager(settings.db_path, scan_fn=scan)))


def test_progress_while_scanning(settings, media_root):
    scan = BlockingScan()
    (media_root / "Tapes").mkdir()
    with blocking_client(settings, scan) as client:
        lib = add(client, "Tapes", media_root / "Tapes")
        client.post(f"/api/libraries/{lib['id']}/scan")
        assert scan.started.wait(5)

        status = client.get("/api/libraries").json()[0]["scan"]
        assert (status["state"], status["done"], status["total"]) == ("scanning", 3, 10)

        # Clicking Scan again while it runs doesn't start a second scan.
        assert client.post(f"/api/libraries/{lib['id']}/scan").json()["state"] == "scanning"
        # The library can't be removed mid-scan, but it can be renamed.
        assert client.delete(f"/api/libraries/{lib['id']}").status_code == 409
        assert client.patch(f"/api/libraries/{lib['id']}", json={"name": "Tapes"}).status_code == 200

        scan.release.set()
        client.app.state.scans.wait_idle()
        status = client.get("/api/libraries").json()[0]["scan"]
        assert (status["state"], status["done"]) == ("done", 10)
        assert scan.runs == 1


def test_scans_queue_one_at_a_time(settings, media_root):
    scan = BlockingScan()
    (media_root / "a").mkdir()
    (media_root / "b").mkdir()
    with blocking_client(settings, scan) as client:
        a = add(client, "a", media_root / "a")
        b = add(client, "b", media_root / "b")
        client.post("/api/libraries/scan")
        assert scan.started.wait(5)
        states = {lib["name"]: lib["scan"]["state"] for lib in client.get("/api/libraries").json()}
        assert states == {"a": "scanning", "b": "queued"}
        scan.release.set()
        client.app.state.scans.wait_idle()
        assert scan.runs == 2
        assert {lib["scan"]["state"] for lib in client.get("/api/libraries").json()} == {"done"}
        assert a["id"] != b["id"]


def test_stopping_drops_queued_scans_and_stops_the_running_one(settings, media_root):
    scan = BlockingScan()
    manager = ScanManager(settings.db_path, scan_fn=scan)
    init_db(settings.db_path)
    manager.start()
    manager._status = {1: {"state": "queued"}, 2: {"state": "queued"}}
    manager._queue.put(1)
    manager._queue.put(2)
    assert scan.started.wait(5)
    began = time.monotonic()
    manager.stop()
    assert time.monotonic() - began < 2
    assert scan.runs == 1                                  # the queued one never ran
    assert manager.status(1)["state"] == manager.status(2)["state"] == "cancelled"


def test_shutting_down_during_a_scan_is_quick(settings, media_root):
    scan = BlockingScan()
    make_dir(media_root / "Tapes")
    with blocking_client(settings, scan) as client:
        lib = client.post("/api/libraries", json={"name": "Tapes", "path": str(media_root / "Tapes")}).json()["id"]
        client.post(f"/api/libraries/{lib}/scan")
        assert scan.started.wait(5)
        began = time.monotonic()
    assert time.monotonic() - began < 2                    # never released: it was stopped


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe not installed")
def test_stopping_kills_a_probe_that_hangs(settings, media_root, monkeypatch):
    """A real scan whose ffprobe never returns (a file that never answers) still stops at once."""
    import os

    from reel import scanner

    folder = make_dir(media_root / "Tapes")
    os.mkfifo(folder / "stuck.mkv")
    monkeypatch.setattr(scanner, "fingerprint", lambda path, size: "fp")   # only ffprobe opens it
    init_db(settings.db_path)
    manager = ScanManager(settings.db_path)      # the real scan, with its own ffprobe supervisor
    app = create_app(settings, manager)
    with TestClient(app) as client:
        lib = client.post("/api/libraries", json={"name": "Tapes", "path": str(folder)}).json()["id"]
        client.post(f"/api/libraries/{lib}/scan")
        for _ in range(100):
            if (client.get("/api/libraries").json()[0]["scan"] or {}).get("state") == "scanning":
                break
            time.sleep(0.05)
        time.sleep(0.5)                                  # ffprobe is now waiting on the file
        began = time.monotonic()
    assert time.monotonic() - began < 3                  # shutdown killed it
    assert manager.status(1)["state"] == "cancelled"


def test_a_failing_clean_up_after_a_scan_doesnt_fail_the_scan(client, media_root):
    make_files(media_root, "Tapes/a.mpg")
    lib = client.post("/api/libraries", json={"name": "Tapes", "path": str(media_root / "Tapes")}).json()["id"]

    def broken(conn):
        raise OSError("images folder unreadable")

    client.scans.after_scan = broken
    client.post(f"/api/libraries/{lib}/scan")
    client.scans.wait_idle()
    body = client.get("/api/libraries").json()[0]
    assert body["scan"]["state"] == "done" and body["last_scan_error"] is None and body["item_count"] == 1


class FlakyDatabase:
    """connect() for the scan manager that fails the first `failures` times."""

    def __init__(self, failures):
        self.failures = failures

    def __call__(self, path, **kwargs):
        from reel.db import connect
        if self.failures:
            self.failures -= 1
            raise sqlite3.OperationalError("unable to open database file")
        return connect(path, **kwargs)


def test_a_database_failure_doesnt_stop_the_scanner(client, media_root, monkeypatch):
    """The connection fails for one scan (and for recording its error): that scan
    reports the failure, and the next one runs normally."""
    from reel import scan_manager

    make_files(media_root, "Tapes/a.mpg")
    lib = client.post("/api/libraries", json={"name": "Tapes", "path": str(media_root / "Tapes")}).json()["id"]
    monkeypatch.setattr(scan_manager, "connect", FlakyDatabase(failures=2))
    client.post(f"/api/libraries/{lib}/scan")
    client.scans.wait_idle()
    status = client.get("/api/libraries").json()[0]["scan"]
    assert status["state"] == "error" and "unable to open database" in status["error"]
    assert client.scans.alive()
    client.post(f"/api/libraries/{lib}/scan")                 # the next scan works
    client.scans.wait_idle()
    body = client.get("/api/libraries").json()[0]
    assert body["scan"]["state"] == "done" and body["item_count"] == 1
    assert client.get("/api/health").json()["scanner"] == {"alive": True, "queued": 0}


def test_an_unexpected_failure_is_contained_too(client, media_root, monkeypatch):
    make_files(media_root, "Tapes/a.mpg")
    lib = client.post("/api/libraries", json={"name": "Tapes", "path": str(media_root / "Tapes")}).json()["id"]
    real = client.scans._scan_one
    calls = []

    def explode_once(library_id):
        calls.append(library_id)
        if len(calls) == 1:
            raise RuntimeError("a bug")
        return real(library_id)

    monkeypatch.setattr(client.scans, "_scan_one", explode_once)
    client.post(f"/api/libraries/{lib}/scan")
    client.scans.wait_idle()
    assert client.get("/api/libraries").json()[0]["scan"]["state"] == "error"
    client.post(f"/api/libraries/{lib}/scan")
    client.scans.wait_idle()
    assert client.get("/api/libraries").json()[0]["scan"]["state"] == "done"


def test_a_dead_scanner_is_restarted_and_reported(settings, media_root):
    make_files(media_root, "Tapes/a.mpg")
    init_db(settings.db_path)
    with TestClient(create_app(settings)) as c:
        lib = c.post("/api/libraries", json={"name": "Tapes", "path": str(media_root / "Tapes")}).json()["id"]
        c.app.state.scans._queue.put(None)                   # the thread ends (as if it had died)
        c.app.state.scans._thread.join(5)
        health = c.get("/api/health")
        assert health.status_code == 503 and health.json()["scanner"]["alive"] is False
        c.post(f"/api/libraries/{lib}/scan")                  # asking for a scan starts it again
        c.app.state.scans.wait_idle()
        assert c.get("/api/libraries").json()[0]["scan"]["state"] == "done"
        assert c.get("/api/health").status_code == 200


# ---- Background work after scans: scans first ----------------------------------------------


def test_background_work_waits_for_the_whole_queue_and_gives_way_to_new_scans(settings):
    """Scan all: every library is scanned before any thumbnail work, and a scan
    queued during that work stops it; unfinished libraries are offered again."""
    events = []

    def scan(conn, library_id, **kwargs):
        events.append(("scan", library_id))
        return {}

    init_db(settings.db_path)
    manager = ScanManager(settings.db_path, scan_fn=scan)
    queued_during_work = threading.Event()

    def after_batch(library_ids, should_stop):
        events.append(("work", tuple(library_ids)))
        if len([e for e in events if e[0] == "work"]) == 1:
            with manager._lock:
                manager._status[3] = {"state": "queued"}
            manager._queue.put(3)                       # someone asks for another scan now
            queued_during_work.set()
            for _ in range(200):
                if should_stop():
                    return False                          # gives way, unfinished
                time.sleep(0.01)
            raise AssertionError("should_stop never turned true")
        return True

    manager.after_batch = after_batch
    manager.start()
    for library_id in (1, 2):
        manager._status[library_id] = {"state": "queued"}
        manager._queue.put(library_id)
    manager.wait_idle()
    manager.stop()
    assert events == [("scan", 1), ("scan", 2), ("work", (1, 2)), ("scan", 3), ("work", (1, 2, 3))]


def test_stopping_ends_a_thumbnail_being_made(tmp_path):
    import os

    from reel.images import Thumbnailer, ThumbnailError

    stuck = tmp_path / "stuck.png"
    os.mkfifo(stuck)                                     # ffmpeg waits on it forever
    thumbs = Thumbnailer(tmp_path / "cache")
    outcome = {}

    def make():
        try:
            thumbs.make(stuck, rev="1-1")
        except ThumbnailError as exc:
            outcome["error"] = str(exc)

    worker = threading.Thread(target=make)
    worker.start()
    for _ in range(50):
        if thumbs._running:
            break
        time.sleep(0.05)
    began = time.monotonic()
    thumbs.stop()
    worker.join(5)
    assert not worker.is_alive() and time.monotonic() - began < 2 and "error" in outcome
    with pytest.raises(ThumbnailError, match="stopping"):
        thumbs.make(tmp_path / "other.png", rev="1-1")


def test_watching_means_a_stream_or_recent_video_requests(monkeypatch):
    from reel.playback import StreamManager, Watching

    streams = StreamManager()
    watching = Watching(streams)
    assert not watching()
    watching.saw_playback()
    assert watching()
    monkeypatch.setattr(Watching, "RECENT", 0.0)
    assert not watching()
    streams.active.add(object())
    assert watching()


def test_playing_a_file_counts_as_watching(client, media_root):
    make_files(media_root, "Tapes/a.mpg")
    lib = client.post("/api/libraries", json={"name": "Tapes", "path": str(media_root / "Tapes")}).json()["id"]
    client.post(f"/api/libraries/{lib}/scan")
    client.scans.wait_idle()
    video = client.get(f"/api/libraries/{lib}/browse").json()["items"][0]["id"]
    assert not client.app.state.watching()
    client.get(f"/api/items/{video}/file")
    assert client.app.state.watching()


@pytest.mark.parametrize("watching, most", [(True, 1), (False, 4)])
def test_scans_probe_one_file_at_a_time_while_someone_watches(watching, most, conn, media_root):
    from reel.libraries import create_library
    from reel.scanner import scan_library

    make_files(media_root, *[f"Tapes/v{i}.mpg" for i in range(10)])
    lib = create_library(conn, media_root, "Media", str(media_root))
    lock = threading.Lock()
    running, peak = [0], [0]

    def probe(path):
        from conftest import FakeProbe
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.05)
        with lock:
            running[0] -= 1
        return FakeProbe.PROFILES[".mpg"]

    scan_library(conn, lib, probe_fn=probe, workers=4, busy=lambda: watching)
    assert peak[0] == most
