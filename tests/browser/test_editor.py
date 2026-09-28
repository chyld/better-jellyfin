"""A video's edit page, and what its ranges do: clips on the video's page that play
just their stretch, and skipped ranges jumped over. In a real browser."""
import os
import time
from pathlib import Path

import pytest

from harness import browser, clips, page, server  # noqa: F401  (fixtures)

pytestmark = pytest.mark.browser
SHOTS = os.environ.get("REEL_SHOTS")   # a folder to save screenshots in, to look at the pages


def shot(page, name):
    if SHOTS:
        import base64
        data = page.cdp("Page.captureScreenshot", format="png", captureBeyondViewport=True)["data"]
        (Path(SHOTS) / f"{name}.png").write_bytes(base64.b64decode(data))


def type_time(page, which, text):
    index = 0 if which == "start" else 1
    page.js(f"""(() => {{ const i = document.querySelectorAll('.edit-time')[{index}];
        i.value = {text!r}; i.dispatchEvent(new Event('change')); return true; }})()""")


def test_a_clip_made_on_the_edit_page_plays_just_its_stretch(server, page):
    page.cdp("Page.bringToFront")
    video = server.videos["long.avi"]                                   # converted: HLS in Chromium
    page.goto(f"{server.base}/#/item/{video}")
    page.wait_for("!!document.querySelector('.btn.edit-link')", message="the video's page")
    assert page.js("document.querySelector('.clips')") is None           # no clips yet
    page.js("document.querySelector('.btn.edit-link').click()")
    page.wait_for("!!document.querySelector('.edit-form')", message="the edit page")
    type_time(page, "start", "0:30")
    type_time(page, "end", "40.5")
    page.js("""(() => { const l = document.querySelector('.edit-meta input');
        l.value = 'The cake'; l.dispatchEvent(new Event('input'));
        document.querySelector('.seg[data-kind=clip]').click(); return true; })()""")
    assert page.js("document.querySelector('.edit-length').textContent") == "Length 0:10.5"
    # The start and end pictures come from the file, at exactly those times.
    page.wait_for("[...document.querySelectorAll('.edit-still')].every(i => i.complete && i.naturalWidth > 0)",
                  message="the pictures")
    srcs = page.js("[...document.querySelectorAll('.edit-still')].map(i => i.getAttribute('src'))")
    assert srcs[0].endswith("at=30.0") and srcs[1].endswith("at=40.5")
    page.js("document.querySelector('.edit-actions .btn.primary').click()")
    page.wait_for("document.querySelectorAll('.range-row').length === 1", message="the saved clip")
    assert "The cake" in page.js("document.querySelector('.range-row').textContent")
    assert page.js("document.querySelector('.edit-actions .btn.primary').textContent") == "Save changes"
    shot(page, "edit-page")
    [saved] = server.call("GET", f"/api/items/{video}/ranges")
    assert (saved["start"], saved["end"], saved["kind"], saved["label"]) == (30.0, 40.5, "clip", "The cake")

    # Done: the video's page lists it under Clips, with its first frame.
    page.js("document.querySelector('.page-head a.btn').click()")
    page.wait_for("!!document.querySelector('.clips .card')", message="the clip on the video's page")
    assert page.js("document.querySelector('.clips .card .label').textContent") == "The cake"
    page.wait_for("document.querySelector('.clips .card img').naturalWidth > 0", message="the clip's picture")
    shot(page, "video-page-with-clip")

    # The clip plays from its start, with a clock and seek bar of its own, and stops at its end.
    page.js("document.querySelector('.clips .card').click()")
    page.wait_for("!!document.querySelector('video.screen')", message="the player")
    assert "The cake" in page.js("document.querySelector('.player-title').textContent")
    page.wait_for("document.querySelector('.clock.total').textContent === '0:11'", message="the clip's length")
    page.playing_past(1, timeout=40)
    page.wait_for("document.querySelector('video.screen').paused && "
                  "document.querySelector('.player-toast').textContent === 'End of clip'",
                  timeout=40, message="the end of the clip")
    raw = page.js("document.querySelector('video.screen').currentTime")
    assert 40 <= raw < 42.5                                              # the video itself stopped at 40.5


def test_a_skipped_range_is_jumped_over(server, page):
    video = server.videos["direct.mp4"]                                # 20 s, played as the file
    server.call("POST", f"/api/items/{video}/ranges", {"start": 2, "end": 14, "kind": "skip"})
    began = time.monotonic()
    page.play(server, "direct.mp4")
    page.wait_for("document.querySelector('video.screen').currentTime >= 14", timeout=10, message="the jump")
    assert time.monotonic() - began < 9                                 # not played through (that's 14 s)
    assert page.js("document.querySelector('.player-toast').textContent").startswith("Skipped 0:02")
    assert page.js("document.querySelectorAll('.seek-skip').length") == 1


def test_the_edit_page_keys_set_the_start_and_end(server, page):
    page.cdp("Page.bringToFront")
    video = server.videos["direct.mp4"]
    page.goto(f"{server.base}/#/edit/{video}")
    page.wait_for("!!document.querySelector('.edit-form') && document.querySelector('.edit-video').readyState >= 1",
                  message="the edit page")
    for _ in range(5):
        page.key("ArrowRight")
    page.key("i")
    page.key("ArrowRight", shift=True)
    page.key(",")
    page.key("o")
    times = page.js("[...document.querySelectorAll('.edit-time')].map(i => i.value)")
    assert times == ["0:05.0", "0:14.9"]
    page.js("document.querySelector('.seg[data-kind=skip]').click()")
    page.js("document.querySelector('.edit-actions .btn.primary').click()")
    page.wait_for("document.querySelectorAll('.edit-band.skip').length === 1", message="the band on the timeline")
    # Delete asks once more.
    page.js("document.querySelector('.range-row .btn.danger').click()")
    assert page.js("document.querySelector('.range-row .btn.danger').textContent") == "Really delete?"
    page.js("document.querySelector('.range-row .btn.danger').click()")
    page.wait_for("document.querySelectorAll('.range-row').length === 0", message="deleted")
    assert server.call("GET", f"/api/items/{video}/ranges") == []


def test_the_pages_on_a_phone(server, page):
    """Nothing wider than the screen on a phone: the edit page and a video's page with clips."""
    video = server.videos["direct.mp4"]
    server.call("POST", f"/api/items/{video}/ranges", {"start": 1, "end": 5, "kind": "clip", "label": "Hello"})
    server.call("POST", f"/api/items/{video}/ranges", {"start": 6, "end": 8, "kind": "skip"})
    server.call("POST", f"/api/items/{video}/ranges", {"start": 9, "end": 12, "label": "Look"})
    page.cdp("Emulation.setDeviceMetricsOverride", width=390, height=844, deviceScaleFactor=1, mobile=True)
    try:
        for url, ready, name in [(f"#/edit/{video}", ".range-row", "edit-phone"),
                                 (f"#/item/{video}", ".clips .card", "video-phone")]:
            page.goto(f"{server.base}/{url}")
            page.wait_for(f"!!document.querySelector('{ready}')", message=name)
            time.sleep(0.5)
            shot(page, name)
            assert page.js("document.documentElement.scrollWidth") <= 390, name
    finally:
        page.cdp("Emulation.setDeviceMetricsOverride", width=1280, height=720, deviceScaleFactor=1, mobile=False)
