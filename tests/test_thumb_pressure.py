"""A folder full of new thumbnails mustn't make the rest of the API wait."""
import concurrent.futures
import json
import socket
import threading
import time
import urllib.request
from functools import partial

import uvicorn

from reel.db import init_db
from reel.images import Thumbnailer
from reel.main import create_app
from reel.scan_manager import ScanManager
from reel.scanner import scan_library

from conftest import make_files


import pytest


@pytest.mark.parametrize("slow", ["making", "nas lookups"])
def test_waiting_thumbnails_dont_hold_up_other_requests(slow, settings, media_root, fake_probe, monkeypatch, tmp_path):
    """Thumbnails stuck on ffmpeg, or on the NAS answering path lookups and stat():
    either way only the thumbnail slots wait, not the threads the API needs."""
    names = [f"Tapes/v{i:02}" for i in range(60)]
    make_files(media_root, *[n + ".mp4" for n in names], *[n + ".png" for n in names])
    jpeg = tmp_path / "t.jpg"
    jpeg.write_bytes(b"\xff\xd8\xff" + b"0" * 100)

    gate = threading.Event()   # thumbnails take as long as the test says (a slow NAS, big PNGs)

    def slow_thumbnail(self, src, shape="poster", **kwargs):
        if slow == "making":
            gate.wait(30)
        return jpeg

    monkeypatch.setattr(Thumbnailer, "cached", lambda self, *args, **kwargs: None)
    monkeypatch.setattr(Thumbnailer, "make", slow_thumbnail)
    if slow == "nas lookups":
        from reel import main
        from reel.paths import resolve_inside

        def slow_resolve(root, rel, **kwargs):
            if str(rel).endswith(".png"):
                gate.wait(30)                              # a hung NAS answering slowly
            return resolve_inside(root, rel, **kwargs)

        monkeypatch.setattr(main, "resolve_inside", slow_resolve)
    init_db(settings.db_path)
    app = create_app(settings, ScanManager(settings.db_path, scan_fn=partial(scan_library, probe_fn=fake_probe)))
    app.state.scans.after_batch = None      # no thumbnails made after the scan: all 60 are requested cold
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"

    def call(method, path, body=None):
        req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body else None,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as res:
            data = res.read()
            return json.loads(data) if data and res.headers.get_content_type() == "application/json" else data

    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.05)
        lib = call("POST", "/api/libraries", {"name": "Tapes", "path": str(media_root / "Tapes")})["id"]
        call("POST", f"/api/libraries/{lib}/scan")
        app.state.scans.wait_idle()
        items = call("GET", f"/api/libraries/{lib}/browse")["items"]
        assert len(items) == 60 and all(i["has_poster"] for i in items)

        with concurrent.futures.ThreadPoolExecutor(61) as pool:
            thumbs = [pool.submit(call, "GET", f"/api/items/{i['id']}/thumb") for i in items]
            time.sleep(0.5)                              # they're all waiting now
            listing = pool.submit(call, "GET", f"/api/libraries/{lib}/browse")
            try:
                answered = bool(listing.result(timeout=2))
            except concurrent.futures.TimeoutError:
                answered = False
            gate.set()                                   # now let the thumbnails finish
            assert all(f.result() for f in thumbs)
        assert answered, "a folder listing waited behind thumbnails"
    finally:
        server.should_exit = True
        thread.join(15)


@pytest.mark.parametrize("playing, most", [(True, 1), (False, 4)])
def test_thumbnails_are_made_one_at_a_time_while_something_plays(playing, most, settings, media_root,
                                                                 fake_probe, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    names = [f"Tapes/v{i}" for i in range(8)]
    make_files(media_root, *[n + ".mp4" for n in names], *[n + ".png" for n in names])
    jpeg = tmp_path / "t.jpg"
    jpeg.write_bytes(b"\xff\xd8\xff" + b"0" * 100)
    lock = threading.Lock()
    running, peak = [0], [0]

    def counting_make(self, src, shape="poster", **kwargs):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.3)
        with lock:
            running[0] -= 1
        return jpeg

    monkeypatch.setattr(Thumbnailer, "cached", lambda self, *args, **kwargs: None)
    monkeypatch.setattr(Thumbnailer, "make", counting_make)
    init_db(settings.db_path)
    app = create_app(settings, ScanManager(settings.db_path, scan_fn=partial(scan_library, probe_fn=fake_probe)))
    app.state.scans.after_batch = None
    with TestClient(app) as c:
        lib = c.post("/api/libraries", json={"name": "Tapes", "path": str(media_root / "Tapes")}).json()["id"]
        c.post(f"/api/libraries/{lib}/scan")
        app.state.scans.wait_idle()
        items = c.get(f"/api/libraries/{lib}/browse").json()["items"]
        if playing:
            app.state.streams.active.add(object())
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            codes = list(pool.map(lambda i: c.get(f"/api/items/{i['id']}/thumb").status_code, items))
        app.state.streams.active.clear()
    assert codes == [200] * 8 and peak[0] == most
