"""Subtitles in the player, in a real browser: drawn from the player's own clock,
so they're right for a file, a repackaged stream after a seek (whose clock
restarts), HLS and a clip; CC (or C) turns them off and on, remembered."""
import pytest

from harness import browser, clips, page, server  # noqa: F401  (fixtures)

pytestmark = pytest.mark.browser

SUBS = {
    "direct.vtt": [(5, 8, "Line at five")],
    "remux.en.vtt": [(2, 5, "Line at two"), (58, 66, "Line at a minute")],
    "long.en.srt": [(124, 132, "Line at two minutes")],          # SRT, converted when it's sent
}


def write(server):
    for name, cues in SUBS.items():
        if name.endswith(".srt"):
            text = "".join(f"{n}\n00:{s // 60:02}:{s % 60:02},000 --> 00:{e // 60:02}:{e % 60:02},000\n{t}\n\n"
                           for n, (s, e, t) in enumerate(cues, 1))
        else:
            text = "WEBVTT\n\n" + "".join(f"00:{s // 60:02}:{s % 60:02}.000 --> 00:{e // 60:02}:{e % 60:02}.000\n{t}\n\n"
                                          for s, e, t in cues)
        (server.media / "Videos" / name).write_text(text)
    server.call("POST", f"/api/libraries/{server.library}/scan")
    server.app.state.scans.wait_idle(60)


SHOWN = "(() => { const b = document.querySelector('.subtitles'); return b && !b.hidden ? b.textContent : ''; })()"
CC = "document.querySelector('button[title*=\"subtitles\" i]')"


def test_subtitles_follow_the_video_whichever_way_it_comes(server, page):
    write(server)
    for name, delivery in (("direct.mp4", "file"), ("remux.mkv", "progressive"), ("long.avi", "hls")):
        params = "video=h264&audio=aac&hls_support=mse"
        assert server.call("GET", f"/api/items/{server.videos[name]}/plan?{params}")["delivery"] == delivery

    page.play(server, "direct.mp4")                                          # the file itself
    page.wait_for(f"{SHOWN} === 'Line at five'", timeout=20, message="the line at 5 s")
    assert 5 <= page.video_state()["t"] < 8.5

    page.play(server, "remux.mkv")                                           # a repackaged stream
    page.wait_for(f"{SHOWN} === 'Line at two'", message="the line at 2 s")
    page.key("ArrowRight", shift=True)                                       # +1 minute: a new stream from ~61 s
    page.wait_for(f"{SHOWN} === 'Line at a minute'", message="the line after the seek")
    state = page.video_state()
    assert state["raw"] < 30 and 58 <= state["t"] < 66                       # its clock restarted; the line is right

    page.goto(f"{server.base}/#/play/{server.videos['long.avi']}?t=122")     # HLS, from an SRT
    page.wait_for(f"{SHOWN} === 'Line at two minutes'", timeout=40, message="the HLS line")


def test_a_clip_shows_its_stretchs_subtitles(server, page):
    write(server)
    video = server.videos["remux.mkv"]
    [clip] = server.call("POST", f"/api/items/{video}/clips", {"start": 57, "end": 65})
    page.cdp("Page.bringToFront")
    page.goto(f"{server.base}/#/play/{video}?clip={clip['id']}")
    page.wait_for(f"{SHOWN} === 'Line at a minute'", timeout=30, message="the clip's line")


def test_cc_turns_them_off_and_on_and_is_remembered(server, page):
    write(server)
    page.play(server, "remux.mkv")
    page.wait_for(f"{SHOWN} === 'Line at two'", message="the line")
    assert page.js(f"{CC}.getAttribute('aria-pressed')") == "true"
    page.js(f"{CC}.click()")                                                 # off
    page.wait_for(f"{SHOWN} === ''", message="hidden")
    assert page.js(f"{CC}.getAttribute('aria-pressed')") == "false"
    page.goto(f"{server.base}/#/")                                           # another video, later: still off
    page.play(server, "direct.mp4")
    page.playing_past(5.5)
    assert page.js(SHOWN) == ""
    page.key("c")                                                            # C: on again
    page.wait_for(f"{SHOWN} === 'Line at five'", message="shown again")


def test_no_cc_button_without_subtitles(server, page):
    page.play(server, "big.avi")
    page.wait_for("!!document.querySelector('.dock')", message="the player")
    assert page.js(f"{CC}.hidden") is True


def test_cards_and_the_video_page_say_cc(server, page):
    write(server)
    page.goto(f"{server.base}/#/library/{server.library}")
    page.wait_for("!!document.querySelector('.type-badge.cc')", message="a CC badge")
    labelled = page.js("[...document.querySelectorAll('.type-badge.cc')].map(b => b.closest('.card').querySelector('.label').textContent).sort()")
    assert labelled == ["direct", "long", "remux"]
    page.goto(f"{server.base}/#/item/{server.videos['remux.mkv']}")
    page.wait_for("!!document.querySelector('.pill.cc')", message="the CC pill")
