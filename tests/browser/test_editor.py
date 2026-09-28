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

    # A clip's card opens its page; Play plays from its start, with a clock of its own,
    # and stops at its end.
    page.js("document.querySelector('.clips .card').click()")
    page.wait_for("!!document.querySelector('.clip-detail')", message="the clip's page")
    assert page.js("document.querySelector('.clip-detail h2').textContent") == "long · Clip 2"
    shot(page, "clip-page")
    page.js("document.querySelector('.clip-detail .btn.play').click()")
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


def test_show_all_lists_each_videos_clips_right_after_it(server, page):
    """Pages of one video (the test forces it), so a video's clips must come with it."""
    video = server.videos["long.avi"]                                  # a video comes after it
    for start in (10, 2):
        server.call("POST", f"/api/items/{video}/clips", {"start": start, "end": start + 5})
    page.cdp("Page.bringToFront")
    page.goto(f"{server.base}/#/")
    page.wait_for("!!document.querySelector('.card')", message="home")
    page.js("""(() => { const real = window.fetch;
        window.fetch = (url, ...rest) => real(String(url).includes('/browse?') ? url + '&limit=1' : url, ...rest);
        return true; })()""")
    page.js(f"location.hash = '#/library/{server.library}?all'")
    total = len(server.videos) + 2
    page.wait_for(f"document.querySelectorAll('.grid.videos .card').length === {total}", message="every video and clip")
    assert page.js("document.querySelector('.summary').textContent") == \
        f"{len(server.videos)} videos and 2 clips in this folder and its subfolders"
    cards = page.js("""[...document.querySelectorAll('.grid.videos .card')].map(c => ({
        href: c.getAttribute('href'), label: c.querySelector('.label').textContent,
        badge: c.querySelector('.type-badge') && c.querySelector('.type-badge').textContent }))""")
    at = next(i for i, c in enumerate(cards) if c["href"].startswith(f"#/item/{video}"))
    assert [c["label"] for c in cards[at + 1:at + 3]] == ["long · Clip 1", "long · Clip 2"]
    assert [c["badge"] for c in cards[at + 1:at + 3]] == ["Clip", "Clip"]
    assert sum(c["badge"] == "Clip" for c in cards) == 2
    page.wait_for(f"document.querySelectorAll('.grid.videos .card')[{at + 1}].querySelector('img').naturalWidth > 0",
                  message="the clip's picture")
    shot(page, "show-all-with-clips")
    # The plain folder view lists them after their video too.
    page.js(f"location.hash = '#/library/{server.library}'")
    page.wait_for("!location.hash.endsWith('?all') && "
                  f"document.querySelectorAll('.grid.videos .card').length === {total}", message="the folder")
    labels = page.js("[...document.querySelectorAll('.grid.videos .card .label')].map(e => e.textContent)")
    assert labels[labels.index("long") + 1:labels.index("long") + 3] == ["long · Clip 1", "long · Clip 2"]
    assert page.js("document.querySelector('.summary').textContent") == f"{len(server.videos)} videos · 2 clips"
    page.js("history.back()")
    page.wait_for(f"location.hash.endsWith('?all') && document.querySelectorAll('.grid.videos .card').length === {total}",
                  message="Show all again")
    # Its video's page steps through the list with the clips: Next is Clip 1.
    page.js(f"document.querySelectorAll('.grid.videos .card')[{at}].click()")
    page.wait_for("!!document.querySelector('.list-pos')", message="the video's page")
    total_count = len(server.videos) + 2
    assert page.js("document.querySelector('.list-pos').textContent") == f"{at + 1} of {total_count}"
    page.js("document.querySelectorAll('.list-nav a')[1].click()")           # Next
    page.wait_for("!!document.querySelector('.clip-detail')", message="the first clip")
    assert page.js("document.querySelector('.clip-detail h2').textContent") == "long · Clip 1"
    assert page.js("document.querySelector('.list-pos').textContent") == f"{at + 2} of {total_count}"
    page.js("document.querySelectorAll('.list-nav a')[1].click()")           # Next
    page.wait_for("(document.querySelector('.clip-detail h2') || {}).textContent === 'long · Clip 2'",
                  message="the second clip")
    page.js("document.querySelectorAll('.list-nav a')[1].click()")           # Next: the next video
    page.wait_for("!!document.querySelector('.detail') && !document.querySelector('.clip-detail')",
                  message="the next video")
    assert page.js("document.querySelector('.list-pos').textContent") == f"{at + 4} of {total_count}"
    page.js("document.querySelectorAll('.list-nav a')[0].click()")           # Prev: back to Clip 2
    page.wait_for("(document.querySelector('.clip-detail h2') || {}).textContent === 'long · Clip 2'",
                  message="back to the second clip")
    # Play plays just the clip; the player's back button goes to the clip's page.
    page.js("document.querySelector('.clip-detail .btn.play').click()")
    page.wait_for("!!document.querySelector('.player-title')", message="the player")
    assert page.js("document.querySelector('.player-title').textContent").endswith("Clip 2")
    page.wait_for("document.querySelector('.clock.total').textContent === '0:05'", message="the clip's length")
    assert page.js("document.querySelector('.player-top a').getAttribute('href')").startswith("#/clip/")
    # Prev/next replace the page in the history: Back returns to the list.
    page.js("history.back()")
    page.wait_for("!!document.querySelector('.clip-detail')", message="the clip's page again")
    page.js("history.back()")
    page.wait_for("location.hash.endsWith('?all') && !!document.querySelector('.grid.videos .card')",
                  message="back at the list")


def test_deleting_a_clip_from_its_page(server, page):
    video = server.videos["direct.mp4"]
    [clip] = server.call("POST", f"/api/items/{video}/clips", {"start": 2, "end": 6})
    page.goto(f"{server.base}/#/item/{video}")
    page.wait_for("!!document.querySelector('.detail')", message="the video's page")
    page.goto(f"{server.base}/#/clip/{clip['id']}")
    page.wait_for("!!document.querySelector('.clip-detail')", message="the clip's page")
    page.js("document.querySelector('.clip-detail .btn.danger').click()")
    page.wait_for("!!document.querySelector('dialog.confirm-dialog[open]')", message="the question")
    page.js("document.querySelector('dialog.confirm-dialog .btn.danger').click()")
    page.wait_for(f"location.hash === '#/item/{video}' && !!document.querySelector('.detail')", message="the video's page")
    assert page.js("document.querySelector('.clips').hidden")
    assert server.call("GET", f"/api/items/{video}/clips") == []
