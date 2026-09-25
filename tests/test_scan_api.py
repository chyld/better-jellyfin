"""The Scan buttons: background scans, progress and errors."""
import shutil
import threading

from fastapi.testclient import TestClient

from reel.db import init_db
from reel.main import create_app
from reel.scan_manager import ScanManager

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

    def __call__(self, conn, library_id, *, workers, on_progress):
        self.runs += 1
        on_progress(3, 10)
        self.started.set()
        assert self.release.wait(10)
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
