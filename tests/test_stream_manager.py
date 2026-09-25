"""The ffmpeg process manager: limits, deadlines, error output and clean-up.

These use small Python scripts in place of ffmpeg, so each behaviour can be
produced on demand.
"""
import asyncio
import socket
import sys
import threading
import time

import pytest
import uvicorn

from reel import playback
from reel.playback import StreamBusy, StreamFailed, StreamLimits, StreamManager

from conftest import make_files


def script(code: str) -> list[str]:
    return [sys.executable, "-c", code]


ENDLESS = script("import sys, time\nwhile True:\n    sys.stdout.write('x' * 1000); sys.stdout.flush(); time.sleep(0.01)")


async def read_all(manager, cmd):
    return b"".join([chunk async for chunk in manager.stream(cmd)])


def test_noisy_error_output_does_not_freeze_the_stream():
    """2 MB on stderr before any video: an unread stderr pipe would block the process."""
    noisy = script("import sys; sys.stderr.write('x' * 2_000_000); sys.stderr.flush(); sys.stdout.write('video')")
    out = asyncio.run(asyncio.wait_for(read_all(StreamManager(), noisy), 10))
    assert out == b"video"


def test_failure_reports_the_last_error_line():
    failing = script("import sys; print('Invalid data found when processing input', file=sys.stderr); sys.exit(1)")
    manager = StreamManager()
    with pytest.raises(StreamFailed, match="Invalid data found"):
        asyncio.run(read_all(manager, failing))
    assert manager.active == set()


def test_one_endless_error_line_is_capped():
    """A huge line with no newline must not stop the draining (readline() would)."""
    manager = StreamManager()
    cmd = script("import sys; sys.stderr.write('E' * 300_000); sys.stderr.flush(); sys.exit(1)")
    with pytest.raises(StreamFailed) as info:
        asyncio.run(asyncio.wait_for(read_all(manager, cmd), 10))
    assert len(str(info.value)) < 700


def test_error_output_is_capped():
    chatty = script("import sys\nfor i in range(10000): print('warning', i, file=sys.stderr)\nsys.exit(1)")
    manager = StreamManager(StreamLimits(error_lines=5))
    with pytest.raises(StreamFailed, match="warning 9999"):
        asyncio.run(read_all(manager, chatty))


def test_nothing_within_the_startup_deadline_is_stopped():
    silent = script("import time; time.sleep(60)")
    manager = StreamManager(StreamLimits(startup_timeout=0.3))
    started = time.monotonic()
    with pytest.raises(StreamFailed, match="no video within"):
        asyncio.run(read_all(manager, silent))
    assert time.monotonic() - started < 5
    assert manager.active == set()


def test_a_stalled_stream_is_stopped():
    stalls = script("import sys, time; sys.stdout.write('start'); sys.stdout.flush(); time.sleep(60)")
    manager = StreamManager(StreamLimits(stall_timeout=0.3))
    started = time.monotonic()
    out = asyncio.run(read_all(manager, stalls))
    assert out == b"start" and time.monotonic() - started < 5
    assert manager.active == set()


def test_streams_are_limited_and_slots_come_back():
    manager = StreamManager(StreamLimits(max_streams=1, wait_for_slot=0.2))

    async def scenario():
        first = manager.stream(ENDLESS)
        await anext(first)
        with pytest.raises(StreamBusy, match="already converting 1"):
            await anext(manager.stream(ENDLESS))
        await first.aclose()                      # e.g. the viewer seeked or left
        second = manager.stream(ENDLESS)
        assert await anext(second)                # the slot is free again
        await second.aclose()

    asyncio.run(scenario())
    assert manager.active == set()


def test_a_waiting_stream_gets_the_slot_as_soon_as_it_frees():
    """A seek closes the old stream a moment after requesting the new one."""
    manager = StreamManager(StreamLimits(max_streams=1, wait_for_slot=5))

    async def scenario():
        old = manager.stream(ENDLESS)
        await anext(old)
        asyncio.get_running_loop().call_later(0.2, lambda: asyncio.ensure_future(old.aclose()))
        new = manager.stream(ENDLESS)
        assert await anext(new)
        await new.aclose()

    asyncio.run(scenario())


def test_shutdown_stops_everything():
    manager = StreamManager()

    async def scenario():
        streams = [manager.stream(ENDLESS) for _ in range(2)]
        for s in streams:
            await anext(s)
        procs = set(manager.active)
        await manager.shutdown()
        assert all(p.returncode is not None for p in procs)
        for s in streams:
            await s.aclose()

    asyncio.run(scenario())
    assert manager.active == set()


# ---- Through the web server ------------------------------------------------------------


def test_busy_server_answers_503(client, media_root, monkeypatch):
    make_files(media_root, "Tapes/a.avi", "Tapes/b.avi")
    lib = client.post("/api/libraries", json={"name": "T", "path": str(media_root / "Tapes")}).json()["id"]
    client.post(f"/api/libraries/{lib}/scan")
    client.scans.wait_idle()
    video = client.get(f"/api/libraries/{lib}/browse").json()["items"][0]["id"]

    async def busy(self, cmd):
        raise StreamBusy("The server is already converting 3 videos. Try again in a moment.")
        yield b""

    monkeypatch.setattr(StreamManager, "stream", busy)
    res = client.get(f"/api/items/{video}/stream")
    assert res.status_code == 503
    assert res.headers["retry-after"] == "5"
    assert "Try again" in res.json()["detail"]


def test_real_disconnect_stops_ffmpeg(settings, media_root, fake_probe, monkeypatch):
    """A real HTTP client that hangs up mid-stream: the process must go away."""
    from functools import partial

    from reel.db import init_db
    from reel.main import create_app
    from reel.scan_manager import ScanManager
    from reel.scanner import scan_library

    make_files(media_root, "Tapes/a.avi", "Tapes/b.avi")
    monkeypatch.setattr(playback, "stream_command", lambda *a, **k: ENDLESS)
    init_db(settings.db_path)
    app = create_app(settings, ScanManager(settings.db_path, scan_fn=partial(scan_library, probe_fn=fake_probe)))

    with socket.socket() as probe_port:
        probe_port.bind(("127.0.0.1", 0))
        port = probe_port.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        import json
        import urllib.request

        base = f"http://127.0.0.1:{port}"

        def call(method, path, body=None):
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(base + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req) as res:
                return json.loads(res.read() or b"null")

        lib = call("POST", "/api/libraries", {"name": "T", "path": str(media_root / "Tapes")})["id"]
        call("POST", f"/api/libraries/{lib}/scan")
        app.state.scans.wait_idle()
        video = call("GET", f"/api/libraries/{lib}/browse")["items"][0]["id"]

        # Raw socket: start the stream, read a little, then hang up.
        sock = socket.create_connection(("127.0.0.1", port))
        sock.sendall(f"GET /api/items/{video}/stream HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        assert b"200 OK" in sock.recv(4096)
        for _ in range(50):
            if app.state.streams.active:
                break
            time.sleep(0.05)
        assert len(app.state.streams.active) == 1
        (proc,) = app.state.streams.active
        sock.close()

        for _ in range(100):
            if not app.state.streams.active:
                break
            time.sleep(0.05)
        assert app.state.streams.active == set()
        assert proc.returncode is not None
    finally:
        server.should_exit = True
        thread.join(10)
