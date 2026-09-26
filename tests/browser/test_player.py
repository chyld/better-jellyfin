"""Playback in a real browser: every delivery, seeking, tabs, expiry, disconnects,
changed files and upload races, against a real server with real ffmpeg."""
import concurrent.futures
import os
import shutil
import time
import urllib.error

import pytest

from reel import hls

from harness import browser, clips, page, page2, server  # noqa: F401  (fixtures)

pytestmark = pytest.mark.browser


def plan(server, name, **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return server.call("GET", f"/api/items/{server.videos[name]}/plan?{query}")


def settle(condition, timeout=10.0):
    """Wait until a server-side condition holds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return condition()


# ---- Each way of sending a video, with sound ------------------------------------------------


def test_direct_file_plays_with_audio(server, page):
    assert plan(server, "direct.mp4")["delivery"] == "file"
    page.play(server, "direct.mp4")
    state = page.playing_past(2)
    assert state["audio"] > 0 and state["video"] > 0


def test_repackaged_stream_plays_with_audio(server, page):
    assert plan(server, "remux.mkv", hls_support="mse")["delivery"] == "progressive"
    page.play(server, "remux.mkv")
    state = page.playing_past(2)
    assert state["audio"] > 0 and state["video"] > 0


def test_converted_hls_plays_with_converted_audio(server, page):
    p = plan(server, "flac.mkv", hls_support="mse")
    assert (p["delivery"], p["audio"]) == ("hls", "encode")      # FLAC can't go in TS segments
    page.play(server, "flac.mkv")
    state = page.playing_past(2)
    assert state["audio"] > 0 and state["video"] > 0


# ---- Seeking -----------------------------------------------------------------------------


def test_repeated_seeking_in_hls(server, page):
    page.play(server, "long.avi")
    page.playing_past(1)
    for _ in range(3):                       # three quick 1-minute jumps: to about 3:00
        page.key("ArrowRight", shift=True)
        time.sleep(0.3)
    state = page.playing_past(181, timeout=40)
    assert state["audio"] > 0
    page.key("Home")                         # and back to the start, from the cache
    page.wait_for("document.querySelector('video.screen').currentTime < 10", message="back at the start")
    page.playing_past(0.5)
    # One viewer, so at most one encoder left running.
    assert settle(lambda: len(server.streams.active) <= 1)


def test_repeated_seeking_in_a_progressive_stream(server, page):
    page.play(server, "remux.mkv")
    page.playing_past(1)
    for _ in range(4):                       # each seek starts a new stream at the new time
        page.key("ArrowRight")               # +10 s
        time.sleep(0.4)
    page.playing_past(41, timeout=30)
    # The streams left behind by each seek were stopped.
    assert settle(lambda: len(server.streams.active) <= 1)


# ---- Leaving, two tabs, a long pause ---------------------------------------------------------


def test_converted_progressive_stream_without_hls(server, page):
    """A browser that plays no HLS gets converted video as one progressive MP4."""
    assert plan(server, "long.avi", hls_support="none")["delivery"] == "progressive"
    page.without_hls()
    page.play(server, "long.avi")
    state = page.playing_past(2)
    assert state["audio"] > 0 and not server.hls.sessions


def test_leaving_the_player_stops_ffmpeg(server, page):
    page.without_hls()                             # a long conversion: ffmpeg is still busy
    page.play(server, "big.avi")
    page.playing_past(1)
    assert server.streams.active
    page.goto(f"{server.base}/#/")                 # the browser drops the stream
    assert settle(lambda: not server.streams.active), "ffmpeg still running after leaving"


def test_two_tabs_at_different_points(server, page, page2):
    page.play(server, "long.avi")
    page.playing_past(1)
    page2.play(server, "long.avi")
    page2.playing_past(1)
    page2.key("ArrowRight", shift=True)
    page2.key("ArrowRight", shift=True)            # the second tab jumps 2 minutes on
    near = page.playing_past(3)
    far = page2.playing_past(121, timeout=40)
    time.sleep(3)                                  # both keep playing side by side
    near2, far2 = page.video_state(), page2.video_state()
    assert near2["t"] > near["t"] and far2["t"] > far["t"]
    assert near2["t"] < 60 < 120 < far2["t"]
    (session,) = server.hls.sessions.values()      # one shared session...
    assert len(session.viewers) == 2               # ...with a viewer per tab
    for viewer in session.viewers.values():        # neither tab's encoder was dragged to the other
        if viewer.encoder is not None:
            assert abs(viewer.encoder.start - viewer.position) <= hls.AHEAD_LIMIT + hls.LOOKAHEAD


def test_playing_on_after_the_session_expired(server, page):
    page.play(server, "long.avi")
    page.playing_past(2)
    page.js("document.querySelector('video.screen').pause()")
    assert server.call("POST", "/_test/expire-hls")["removed"] == 1
    assert not server.hls.sessions
    page.key("ArrowRight", shift=True)             # far past what's buffered: new segments
    page.js("document.querySelector('video.screen').play()")
    page.playing_past(62, timeout=40)
    assert len(server.hls.sessions) == 1           # made again from the segment URLs


# ---- A file replaced while it plays ---------------------------------------------------------


def test_file_replaced_while_playing_reloads_and_carries_on(server, page, clips):
    page.play(server, "long.avi")
    page.playing_past(2)
    (old,) = server.hls.sessions.values()
    server.call("POST", "/_test/forget-hls?start=30")   # from 3:00 on, nothing is encoded
    path = server.media / "Videos/long.avi"
    shutil.copy(clips / "long.avi", path)          # a new copy of the file: a new version
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    for _ in range(4):                             # to about 4:00: needs a new encoder
        page.key("ArrowRight", shift=True)
        time.sleep(0.2)
    page.playing_past(241, timeout=40)
    assert old.retired and old.sid not in server.hls.sessions
    assert len(server.hls.sessions) == 1           # the player reloaded onto the new version


# ---- Uploads racing each other ---------------------------------------------------------------


def picture_bytes(tmp_path, colour: str) -> bytes:
    import subprocess
    out = tmp_path / f"{colour}.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={colour}:s=300x450",
                    "-frames:v", "1", str(out)], check=True)
    return out.read_bytes()


def test_simultaneous_first_folder_uploads(server, tmp_path):
    """Several uploads to a folder that has no picture yet, all at once: whichever
    wins, the folder ends up with a picture that exists."""
    pictures = [picture_bytes(tmp_path, c) for c in ("red", "green", "blue", "white", "black", "yellow")]
    (server.media / "Videos/Sub").mkdir()
    shutil.copy(server.media / "Videos/direct.mp4", server.media / "Videos/Sub/clip.mp4")
    server.call("POST", f"/api/libraries/{server.library}/scan")
    server.app.state.scans.wait_idle(60)
    url = f"/api/libraries/{server.library}/folder-image?path=Sub"

    def upload(data):
        try:
            return server.call("PUT", url, raw=data)
        except urllib.error.HTTPError as err:
            return err.code

    with concurrent.futures.ThreadPoolExecutor(len(pictures)) as pool:
        results = list(pool.map(upload, pictures))
    assert all(isinstance(r, dict) for r in results), results
    folder = next(f for f in server.call("GET", f"/api/libraries/{server.library}/browse")["folders"]
                  if f["name"] == "Sub")
    assert folder["custom_art"]
    art = server.call("GET", f"/api/libraries/{server.library}/folder-art?path=Sub&v={folder['custom_art']}")
    assert art[:3] == b"\xff\xd8\xff"               # a JPEG came back


# ---- Snapshots -------------------------------------------------------------------------


def colour_of(image: bytes, tmp_path) -> str:
    import subprocess
    src = tmp_path / "shot.img"
    src.write_bytes(image)
    rgb = subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-vf", "scale=1:1", "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "pipe:1"], capture_output=True, check=True).stdout
    return "red" if rgb[0] > rgb[2] else "blue"


def snap(page) -> str:
    """Press P and wait for the player to say how it went."""
    page.key("p")
    return page.wait_for("""(() => { const t = document.querySelector('.player-toast');
        return t && !t.hidden && !t.textContent.startsWith('Saving') && t.textContent; })()""",
                         message="the snapshot result")


def test_snapshot_button_takes_the_frame_on_screen(server, page, tmp_path):
    """In a progressive stream (whose clock restarts at each seek), before and after a seek."""
    video = server.videos["colours.mkv"]
    page.play(server, "colours.mkv")
    page.playing_past(2)
    assert snap(page).startswith("Preview updated")
    first = server.call("GET", f"/api/items/{video}")["custom_image"]
    assert colour_of(server.call("GET", f"/api/items/{video}/thumb?v={first}"), tmp_path) == "red"

    page.js("document.querySelector('video.screen').play()")
    for _ in range(4):
        page.key("ArrowRight")                          # +10 s each: to about 0:42
        time.sleep(0.4)
    page.playing_past(35)
    assert snap(page).startswith("Preview updated")
    second = server.call("GET", f"/api/items/{video}")["custom_image"]
    assert second != first
    assert colour_of(server.call("GET", f"/api/items/{video}/thumb?v={second}"), tmp_path) == "blue"
    assert page.video_state()["paused"]                 # taking it paused the video

    page.goto(f"{server.base}/#/item/{video}")          # the video page shows the new picture
    src = page.wait_for("(document.querySelector('.hero-wrap img') || {}).src", message="the preview")
    assert f"v={second}" in src


# ---- Marks -----------------------------------------------------------------------------


def test_mark_a_spot_then_jump_to_it_from_the_video_page(server, page):
    video = server.videos["remux.mkv"]                     # a progressive stream: starting at t matters
    page.play(server, "remux.mkv")
    page.playing_past(3)
    page.js("document.querySelector('button[aria-label=\"Mark this spot\"]').click()")
    page.wait_for("/Marked/.test((document.querySelector('.player-toast') || {}).textContent || '')",
                  message="the mark's note")
    assert page.js("document.querySelectorAll('.seek-mark').length") == 1      # a tick on the seek bar
    marked = server.call("GET", f"/api/items/{video}/marks")
    assert len(marked) == 1 and marked[0]["time"] >= 3

    page.goto(f"{server.base}/#/item/{video}")
    link = page.wait_for("(document.querySelector('.marks-section a') || {}).getAttribute?.('href')",
                         message="the marks list")
    assert link == f"#/play/{video}?t={marked[0]['time']}"
    page.js("document.querySelector('.marks-section a').click()")
    state = page.playing_past(marked[0]["time"] - 0.5)       # starts at the mark, not at 0
    assert state["t"] < marked[0]["time"] + 5

    page.goto(f"{server.base}/#/item/{video}")
    page.wait_for("!!document.querySelector('.marks-section .chip-x')", message="the delete button")
    page.js("document.querySelector('.marks-section .chip-x').click()")
    page.wait_for("document.querySelector('.marks-section').hidden", message="the empty list hidden")
    assert server.call("GET", f"/api/items/{video}/marks") == []


def test_a_play_link_with_a_start_time(server, page):
    video = server.videos["long.avi"]                       # HLS
    page.goto(f"{server.base}/#/play/{video}?t=125")
    page.wait_for("!!document.querySelector('video.screen')", message="the player")
    page.cdp("Page.bringToFront")
    state = page.playing_past(125)
    assert state["t"] < 140
