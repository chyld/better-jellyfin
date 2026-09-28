"""A video's edit page (mark twice, make a clip), and its clips: listed on the
video's page and the edit page, deleted only after confirming, and each playing
just its stretch. In a real browser."""
import base64
import os
import time
from pathlib import Path

import pytest

from harness import browser, clips, page, server  # noqa: F401  (fixtures)

pytestmark = pytest.mark.browser
SHOTS = os.environ.get("REEL_SHOTS")   # a folder to save screenshots in, to look at the pages


def shot(page, name, whole=True):
    if SHOTS:
        data = page.cdp("Page.captureScreenshot", format="png", captureBeyondViewport=whole)["data"]
        (Path(SHOTS) / f"{name}.png").write_bytes(base64.b64decode(data))


def open_editor(server, page, name):
    page.cdp("Page.bringToFront")                    # Chrome won't load media in a tab never in front
    page.goto(f"{server.base}/#/edit/{server.videos[name]}")
    page.wait_for("!!document.querySelector('.edit-player') && document.querySelector('.edit-video').readyState >= 1",
                  message="the edit page")


def go_to(page, seconds):
    page.js(f"document.querySelector('.edit-video').currentTime = {seconds}")
    page.wait_for(f"Math.abs(document.querySelector('.edit-video').currentTime - {seconds}) < 0.05", message=f"{seconds}s")


def buttons(page):
    return page.js("""({mark: document.querySelector('.mark-btn').disabled,
        clear: [...document.querySelectorAll('.edit-buttons .btn')][1].disabled,
        make: document.querySelector('.edit-buttons .btn.primary').disabled,
        ticks: document.querySelectorAll('.edit-mark').length})""")


def clip_names(page):
    return page.js("[...document.querySelectorAll('.clips .card .label')].map(e => e.textContent)")


def test_marking_twice_makes_a_clip_listed_everywhere(server, page):
    video = server.videos["long.avi"]                                   # converted: HLS in Chromium
    page.cdp("Page.bringToFront")
    page.goto(f"{server.base}/#/item/{video}")
    page.wait_for("!!document.querySelector('.btn.edit-link')", message="the video's page")
    assert page.js("document.querySelector('.clips').hidden")              # no clips yet
    page.js("document.querySelector('.btn.edit-link').click()")
    page.wait_for("!!document.querySelector('.edit-player') && document.querySelector('.edit-video').readyState >= 1",
                  message="the edit page")
    assert buttons(page) == {"mark": False, "clear": True, "make": True, "ticks": 0}
    assert page.js("document.querySelector('.clips .summary').textContent") == "No clips yet."

    go_to(page, 30)
    page.js("document.querySelector('.mark-btn').click()")
    assert buttons(page) == {"mark": False, "clear": False, "make": True, "ticks": 1}
    assert page.js("document.querySelector('.edit-status').textContent").startswith("Marked 0:30.0.")
    go_to(page, 40)
    page.key("m")                                                          # the key does the same
    assert buttons(page) == {"mark": True, "clear": False, "make": False, "ticks": 2}
    page.key("m")                                                          # never more than two
    assert buttons(page)["ticks"] == 2
    shot(page, "edit-two-marks")
    page.js("document.querySelector('.edit-buttons .btn.primary').click()")
    page.wait_for("document.querySelectorAll('.clips .card').length === 1", message="the clip")
    assert buttons(page) == {"mark": False, "clear": True, "make": True, "ticks": 0}   # marks cleared
    assert page.js("document.querySelector('.edit-status').textContent").startswith("Clip 1 made.")

    # Marks the other way round, then another clip; and Clear marks starts over.
    go_to(page, 50)
    page.key("m")
    page.js("[...document.querySelectorAll('.edit-buttons .btn')][1].click()")
    assert buttons(page)["ticks"] == 0
    go_to(page, 50)
    page.key("m")
    go_to(page, 45)
    page.key("m")
    page.js("document.querySelector('.edit-buttons .btn.primary').click()")
    page.wait_for("document.querySelectorAll('.clips .card').length === 2", message="the second clip")
    assert clip_names(page) == ["Clip 1", "Clip 2"]
    assert page.js("document.querySelectorAll('.edit-band').length") == 2
    page.wait_for("[...document.querySelectorAll('.clips .card img')].every(i => i.complete && i.naturalWidth > 0)",
                  message="the clips' pictures")
    shot(page, "edit-page")
    stored = [(c["name"], c["start"], c["end"]) for c in server.call("GET", f"/api/items/{video}/clips")]
    assert stored == [("Clip 1", 30.0, 40.0), ("Clip 2", 45.0, 50.0)]

    # Done: the video's page lists them too. Delete asks first; Cancel keeps the clip.
    page.js("document.querySelector('.page-head a.btn').click()")
    page.wait_for("document.querySelectorAll('.clips .card').length === 2", message="the clips on the video's page")
    assert clip_names(page) == ["Clip 1", "Clip 2"]
    shot(page, "video-page-with-clips")
    page.js("document.querySelector('.clip-delete').click()")
    page.wait_for("!!document.querySelector('dialog.confirm-dialog[open]')", message="the question")
    assert page.js("document.querySelector('dialog.confirm-dialog h3').textContent") == "Delete Clip 1?"
    time.sleep(0.4)                                                        # its fade-in
    shot(page, "delete-clip-dialog", whole=False)
    page.js("document.querySelector('dialog.confirm-dialog .btn:not(.danger)').click()")
    page.wait_for("!document.querySelector('dialog.confirm-dialog')", message="cancelled")
    assert clip_names(page) == ["Clip 1", "Clip 2"] and len(server.call("GET", f"/api/items/{video}/clips")) == 2
    page.js("document.querySelector('.clip-delete').click()")
    page.wait_for("!!document.querySelector('dialog.confirm-dialog[open]')", message="the question again")
    page.js("document.querySelector('dialog.confirm-dialog .btn.danger').click()")
    page.wait_for("document.querySelectorAll('.clips .card').length === 1", message="deleted")
    assert clip_names(page) == ["Clip 2"]
    assert [c["name"] for c in server.call("GET", f"/api/items/{video}/clips")] == ["Clip 2"]

    # A clip plays from its start, with a clock of its own, and stops at its end.
    page.js("document.querySelector('.clips .card').click()")
    page.wait_for("!!document.querySelector('video.screen')", message="the player")
    assert page.js("document.querySelector('.player-title').textContent").endswith("Clip 2")
    page.wait_for("document.querySelector('.clock.total').textContent === '0:05'", message="the clip's length")
    page.playing_past(0.5, timeout=40)
    page.wait_for("document.querySelector('video.screen').paused && "
                  "document.querySelector('.player-toast').textContent === 'End of clip'",
                  timeout=40, message="the end of the clip")
    assert 49.5 <= page.js("document.querySelector('video.screen').currentTime") < 51


def test_marks_too_close_together_make_no_clip(server, page):
    open_editor(server, page, "direct.mp4")
    go_to(page, 5)
    page.key("m")
    go_to(page, 5.2)
    page.key("m")
    assert buttons(page) == {"mark": True, "clear": False, "make": True, "ticks": 2}
    assert "only 0:00.2 apart" in page.js("document.querySelector('.edit-status').textContent")


def test_the_pages_on_a_phone(server, page):
    """Nothing wider than the screen on a phone: the edit page and a video's page with clips."""
    video = server.videos["direct.mp4"]
    for start in (1, 6):
        server.call("POST", f"/api/items/{video}/clips", {"start": start, "end": start + 4})
    page.cdp("Emulation.setDeviceMetricsOverride", width=390, height=844, deviceScaleFactor=1, mobile=True)
    try:
        for url, ready, name in [(f"#/edit/{video}", ".clips .card", "edit-phone"),
                                 (f"#/item/{video}", ".clips .card", "video-phone")]:
            page.goto(f"{server.base}/{url}")
            page.wait_for(f"!!document.querySelector('{ready}')", message=name)
            time.sleep(0.5)
            shot(page, name)
            assert page.js("document.documentElement.scrollWidth") <= 390, name
    finally:
        page.cdp("Emulation.setDeviceMetricsOverride", width=1280, height=720, deviceScaleFactor=1, mobile=False)
