"""Harness for the browser tests: a real Reel server, real ffmpeg, and headless Chromium over CDP.

They're slow, so they only run when asked:  uv run pytest -m browser
Everything here is made up on the fly (generated test clips in temp folders).
"""
import base64
import json
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import pytest
import uvicorn
from websockets.sync.client import connect as ws_connect

from reel.config import Settings
from reel.db import init_db
from reel.main import create_app
from reel.scan_manager import ScanManager


CHROMIUM = next((shutil.which(n) for n in ("chromium", "chromium-browser", "google-chrome") if shutil.which(n)), None)
if not (CHROMIUM and shutil.which("ffmpeg") and shutil.which("ffprobe")):
    pytest.skip("browser tests need Chromium and ffmpeg", allow_module_level=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---- Clips -------------------------------------------------------------------------------

# name -> (seconds, frame size, ffmpeg output arguments). Mostly small frames, so
# encoding is quick; "big" is converted slowly enough to still be running mid-test.
CLIPS = {
    "direct.mp4": (20, "320x180", ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"]),
    "remux.mkv": (90, "320x180", ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"]),
    "flac.mkv": (60, "320x180", ["-c:v", "mpeg4", "-c:a", "flac"]),
    "long.avi": (300, "320x180", ["-c:v", "mpeg4", "-c:a", "libmp3lame"]),
    "big.avi": (600, "1280x720", ["-c:v", "mpeg4", "-q:v", "8", "-c:a", "libmp3lame"]),
    # Red for the first 30 seconds, blue after: shows which moment a snapshot is from.
    "colours.mkv": (60, "320x180", ["-vf", "drawbox=x=0:y=0:w=iw:h=ih:color=red:t=fill,"
                                           "drawbox=x=0:y=0:w=iw:h=ih:color=blue:t=fill:enable='gte(t,30)'",
                                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"]),
}


@pytest.fixture(scope="session")
def clips(tmp_path_factory) -> Path:
    folder = tmp_path_factory.mktemp("browser-clips")
    for name, (seconds, size, args) in CLIPS.items():
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=15:duration={seconds}",
             "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-shortest", *args, str(folder / name)],
            check=True,
        )
    return folder


# ---- The server --------------------------------------------------------------------------


class Server:
    def __init__(self, app, port: int, media: Path):
        self.app, self.port, self.media = app, port, media
        self.base = f"http://127.0.0.1:{port}"
        self.videos: dict[str, str] = {}   # file name -> video id
        self.library = ""

    def call(self, method: str, path: str, body=None, raw: bytes | None = None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as res:
            body = res.read()
            return json.loads(body) if body and res.headers.get("content-type", "").startswith("application/json") else body

    @property
    def hls(self):
        return self.app.state.hls

    @property
    def streams(self):
        return self.app.state.streams


@pytest.fixture
def server(tmp_path, clips):
    media = tmp_path / "media"
    (media / "Videos").mkdir(parents=True)
    for name in CLIPS:
        shutil.copy(clips / name, media / "Videos" / name)
    settings = Settings(media_root=media.resolve(), data_dir=tmp_path / "data", probe_workers=2)
    settings.data_dir.mkdir()
    init_db(settings.db_path)
    app = create_app(settings, ScanManager(settings.db_path, workers=2))

    async def expire_hls():
        """Test hook: drop every HLS viewer and session, as after a long pause."""
        return {"removed": await app.state.hls.remove_idle(0)}

    app.add_api_route("/_test/expire-hls", expire_hls, methods=["POST"])
    app.router.routes.insert(0, app.router.routes.pop())   # ahead of the static files at "/"

    port = free_port()
    uv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=uv.run, daemon=True)
    thread.start()
    for _ in range(200):
        if uv.started:
            break
        time.sleep(0.05)
    srv = Server(app, port, media)
    srv.library = srv.call("POST", "/api/libraries", {"name": "Videos", "path": str(media / "Videos")})["id"]
    srv.call("POST", f"/api/libraries/{srv.library}/scan")
    app.state.scans.wait_idle(60)
    items = srv.call("GET", f"/api/libraries/{srv.library}/browse")["items"]
    titles = {Path(n).stem: n for n in CLIPS}
    srv.videos = {titles[i["title"]]: i["id"] for i in items}
    try:
        yield srv
    finally:
        uv.should_exit = True
        thread.join(15)


# ---- The browser -------------------------------------------------------------------------


class Page:
    """One Chromium tab, driven over the DevTools protocol."""

    def __init__(self, browser: "Browser", target: dict):
        self.browser, self.target = browser, target
        self._connection = ws_connect(target["webSocketDebuggerUrl"], max_size=None, open_timeout=10)
        self.ws = self._connection.__enter__()
        self.n = 0
        self.cdp("Page.enable")
        self.cdp("Emulation.setDeviceMetricsOverride", width=1280, height=720, deviceScaleFactor=1, mobile=False)

    def cdp(self, method: str, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv(timeout=60))
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def js(self, expression: str):
        result = self.cdp("Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in result:
            raise RuntimeError(f"JS error in {expression!r}: {result['exceptionDetails']}")
        return result.get("result", {}).get("value")

    def without_hls(self) -> None:
        """Pretend this browser can't play HLS at all (no MediaSource for hls.js, no
        native HLS), so converted video comes as the progressive stream. Applies to
        pages loaded after this."""
        self.cdp("Page.addScriptToEvaluateOnNewDocument", source="""
            for (const k of ['MediaSource', 'ManagedMediaSource', 'WebKitMediaSource'])
              Object.defineProperty(window, k, {value: undefined, configurable: true});
            const canPlay = HTMLMediaElement.prototype.canPlayType;
            HTMLMediaElement.prototype.canPlayType = function (type) {
              return /mpegurl/i.test(type) ? '' : canPlay.call(this, type);
            };""")

    def goto(self, url: str) -> None:
        self.cdp("Page.navigate", url=url)

    def wait_for(self, expression: str, timeout: float = 20, message: str = ""):
        """Poll until the expression is truthy; returns its value."""
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = self.js(expression)
            if last:
                return last
            time.sleep(0.2)
        raise AssertionError(f"timed out waiting for {message or expression} (last value: {last!r})")

    # -- the player --

    def video_state(self) -> dict:
        return self.js("""(() => {
            const v = document.querySelector("video.screen");
            const m = document.querySelector(".player-message");
            if (!v) return null;
            // The player's own position: a progressive stream restarts currentTime at 0.
            const bar = document.querySelector(".seek[role=slider]");
            const t = bar && bar.hasAttribute("aria-valuenow") ? Number(bar.getAttribute("aria-valuenow")) : v.currentTime;
            return {t, raw: v.currentTime, paused: v.paused, audio: v.webkitAudioDecodedByteCount,
                    video: v.webkitVideoDecodedByteCount, error: v.error && v.error.message,
                    message: m && !m.hidden ? m.textContent : null};
        })()""")

    def play(self, server: Server, name: str) -> None:
        # Chrome won't load media in a tab that has never been in front.
        self.cdp("Page.bringToFront")
        self.goto(f"{server.base}/#/play/{server.videos[name]}")
        self.wait_for("!!document.querySelector('video.screen')", message="the player")

    def playing_past(self, seconds: float, timeout: float = 30) -> dict:
        """Wait until the video is past `seconds` and really playing: its clock
        moves on (right after a seek the position already shows the target)."""
        deadline = time.monotonic() + timeout
        state = None
        while time.monotonic() < deadline:
            state = self.video_state()
            assert state is None or not state["message"], f"player error: {state['message']}"
            if state and state["t"] > seconds and not state["paused"]:
                time.sleep(0.6)
                later = self.video_state()
                if later["raw"] > state["raw"] and later["t"] > seconds:
                    return later
            time.sleep(0.2)
        raise AssertionError(f"not playing past {seconds}s: {state}")

    def key(self, key: str, shift: bool = False) -> None:
        self.js(f"document.dispatchEvent(new KeyboardEvent('keydown', {{key: {key!r}, shiftKey: {str(shift).lower()}}}))")

    def screenshot(self, path: Path) -> None:
        path.write_bytes(base64.b64decode(self.cdp("Page.captureScreenshot", format="png")["data"]))

    def close(self) -> None:
        try:
            self._connection.__exit__(None, None, None)
        finally:
            self.browser.close_target(self.target["id"])


class Browser:
    def __init__(self, profile: Path):
        self.port = free_port()
        self.proc = subprocess.Popen(
            [CHROMIUM, "--headless=new", "--no-sandbox", "--mute-audio", "--hide-scrollbars",
             # Tabs in the background keep playing at full speed (two-tab tests).
             "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
             "--disable-renderer-backgrounding",
             "--autoplay-policy=no-user-gesture-required", f"--remote-debugging-port={self.port}",
             f"--user-data-dir={profile}", "--disable-extensions", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(100):
            try:
                self._json("version")
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("Chromium didn't start")

    def _json(self, path: str, method: str = "GET"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/json/{path}", method=method)
        with urllib.request.urlopen(req, timeout=10) as res:
            body = res.read()
            return json.loads(body) if body.strip().startswith((b"{", b"[")) else body

    def new_page(self) -> Page:
        return Page(self, self._json("new?about:blank", method="PUT"))

    def close_target(self, target_id: str) -> None:
        try:
            self._json(f"close/{target_id}")
        except OSError:
            pass

    def quit(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


@pytest.fixture(scope="session")
def browser(tmp_path_factory):
    b = Browser(tmp_path_factory.mktemp("chromium-profile"))
    yield b
    b.quit()


@pytest.fixture
def page(browser):
    p = browser.new_page()
    yield p
    p.close()


@pytest.fixture
def page2(browser):
    p = browser.new_page()
    yield p
    p.close()
