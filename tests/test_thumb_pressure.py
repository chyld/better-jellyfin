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


def test_waiting_thumbnails_dont_hold_up_other_requests(settings, media_root, fake_probe, monkeypatch, tmp_path):
    names = [f"Tapes/v{i:02}" for i in range(60)]
    make_files(media_root, *[n + ".mp4" for n in names], *[n + ".png" for n in names])
    jpeg = tmp_path / "t.jpg"
    jpeg.write_bytes(b"\xff\xd8\xff" + b"0" * 100)

    gate = threading.Event()   # thumbnails take as long as the test says (a slow NAS, big PNGs)

    def slow_thumbnail(self, src, shape="poster"):
        gate.wait(30)
        return jpeg

    monkeypatch.setattr(Thumbnailer, "cached", lambda self, src, shape="poster": None, raising=False)
    monkeypatch.setattr(Thumbnailer, "from_image", slow_thumbnail)
    init_db(settings.db_path)
    app = create_app(settings, ScanManager(settings.db_path, scan_fn=partial(scan_library, probe_fn=fake_probe)))
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
