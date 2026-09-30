"""Pages other than the player, in a real browser."""
import pytest

from harness import browser, clips, page, server  # noqa: F401  (fixtures)

pytestmark = pytest.mark.browser


def test_libraries_page_survives_quick_back_and_forth(server, page):
    """Libraries, Home, Libraries in quick succession: one working page, one list."""
    page.goto(f"{server.base}/#/manage")
    page.wait_for("document.querySelectorAll('.library-list .library').length === 1", message="the list")
    page.js("location.hash = '#/'; location.hash = '#/manage'")
    page.wait_for("document.querySelectorAll('.library-list').length === 1 && "
                  "document.querySelectorAll('.library-list .library').length === 1", message="one list")
    names = page.js("[...document.querySelectorAll('.library h3')].map(e => e.textContent)")
    assert names == ["Videos"]
    page.js("document.querySelector('.library .btn').click()")        # Scan: polls while busy
    page.wait_for("/Scan finished|Waiting|Scanning/.test(document.querySelector('.library .meta').textContent)",
                  message="the scan status")


def test_a_slow_replaced_player_leaves_the_current_one_alone(server, page):
    """Player A loads slowly; player B opens meanwhile. When A finally finishes (and
    is cleaned up at once), B keeps its page-wide state."""
    import time

    page.goto(f"{server.base}/#/")
    page.wait_for("!!document.querySelector('.card')", message="home")
    # Hold the first /plan answer back for 2 seconds.
    page.js("""(() => {
        const real = window.fetch; let held = false;
        window.fetch = (url, ...rest) => {
            if (!held && String(url).includes('/plan')) {
                held = true;
                return new Promise((r) => setTimeout(r, 2000)).then(() => real(url, ...rest));
            }
            return real(url, ...rest);
        };
        return true;
    })()""")
    a, b = server.videos["long.avi"], server.videos["direct.mp4"]
    page.js(f"location.hash = '#/play/{a}'")
    time.sleep(0.2)
    page.js(f"location.hash = '#/play/{b}'")
    page.playing_past(0.5)
    time.sleep(2.5)                                        # A has finished loading and been cleaned up
    assert page.js("document.body.classList.contains('playing')")
    assert page.js("document.querySelectorAll('video.screen').length") == 1
    assert page.video_state()["paused"] is False


def test_a_failed_page_of_videos_offers_a_retry(server, page):
    """Pages of 2 (the test forces it); the second page fails once: the folder says
    so and loads it on Retry, instead of silently stopping."""
    page.goto(f"{server.base}/#/")
    page.wait_for("!!document.querySelector('.card')", message="home")
    page.js("""(() => {
        const real = window.fetch; let failed = false;
        window.fetch = (url, ...rest) => {
            url = String(url);
            if (url.includes('/browse?')) {
                url += '&limit=2';
                if (!failed && url.includes('offset=2')) {
                    failed = true;
                    return Promise.resolve(new Response(JSON.stringify({detail: 'Server hiccup'}), {status: 500}));
                }
            }
            return real(url, ...rest);
        };
        return true;
    })()""")
    page.js(f"location.hash = '#/library/{server.library}'")
    page.wait_for("(document.querySelector('.load-error') || {hidden: true}).hidden === false", message="the error")
    assert "Server hiccup" in page.js("document.querySelector('.load-error').textContent")
    assert page.js("document.querySelectorAll('.grid.videos li').length") == 2
    page.js("document.querySelector('.load-error button').click()")
    total = len(server.videos)
    page.wait_for(f"document.querySelectorAll('.grid.videos li').length === {total}", message="all videos")
    assert page.js("document.querySelector('.load-error').hidden")


def test_prev_next_through_a_show_all_list(server, page):
    """A video opened from "Show all" steps through that list; Back then returns to
    the list, however many steps were taken. Opened any other way: no prev/next."""
    total = len(server.videos)
    page.goto(f"{server.base}/#/library/{server.library}")
    page.wait_for("!!document.querySelector('.grid.videos .card')", message="the folder")
    first = page.js("document.querySelector('.grid.videos .card').getAttribute('href')")
    assert "?" not in first                                                   # a plain folder
    page.js(f"location.hash = '#/library/{server.library}?all'")
    page.wait_for("!!document.querySelector('.grid.videos .card .where')", message="the show-all list")
    # At the library's top, a video's path from there is its whole path (the clips sit at the top).
    assert page.js("document.querySelector('.grid.videos .card .where').textContent") in server.videos
    page.js("document.querySelector('.grid.videos .card').click()")
    page.wait_for("!!document.querySelector('.list-nav')", message="prev/next")
    assert page.js("document.querySelector('.list-pos').textContent") == f"1 of {total}"
    assert page.js("document.querySelector('.list-nav button').disabled")     # no previous
    page.js("document.querySelector('.list-nav a').click()")                  # Next
    page.wait_for(f"(document.querySelector('.list-pos') || {{}}).textContent === '2 of {total}'", message="the 2nd")
    page.js("document.querySelectorAll('.list-nav a')[1].click()")            # Next again
    page.wait_for(f"(document.querySelector('.list-pos') || {{}}).textContent === '3 of {total}'", message="the 3rd")
    assert "all=" in page.js("document.querySelector('.btn.play').getAttribute('href')")
    page.js("history.back()")
    page.wait_for("location.hash.endsWith('?all') && !!document.querySelector('.grid.videos .card')",
                  message="back at the list")
    page.js(f"location.hash = {first!r}")                                      # the same video, directly
    page.wait_for("!!document.querySelector('.detail')", message="the video's page")
    assert page.js("document.querySelector('.list-nav')") is None


def test_cards_look_the_same_and_colour_like_the_video_page(server, page):
    """A folder's cards and its Show all cards have the same lines; each card's format
    badge has the tone of the play-mode pill on that video's page, in this browser."""
    shape = """[...document.querySelectorAll('.grid.videos .card')].map(c => ({
        href: c.getAttribute('href').split('?')[0],
        lines: [...c.children].filter(e => e.matches('.label, .sub')).map(e => e.className),
        type: c.querySelector('.type-badge').textContent,
        tone: ['ok', 'info', 'warn', 'err'].find(t => c.querySelector('.type-badge').classList.contains(t)) }))"""
    page.goto(f"{server.base}/#/library/{server.library}")
    page.wait_for("!!document.querySelector('.grid.videos .type-badge')", message="the folder")
    folder = page.js(shape)
    page.js(f"location.hash = '#/library/{server.library}?all'")
    page.wait_for("location.hash.endsWith('?all') && !!document.querySelector('.grid.videos .type-badge')",
                  message="the show-all list")
    listed = page.js(shape)
    assert len(folder) == len(server.videos)
    assert {c["lines"] == ["label", "sub where", "sub meta"] for c in folder + listed} == {True}
    assert sorted(folder, key=lambda c: c["href"]) == sorted(listed, key=lambda c: c["href"])
    by_id = {c["href"].rsplit("/", 1)[-1]: c for c in folder}
    tones = {name: by_id[uid]["tone"] for name, uid in server.videos.items()}
    assert tones["direct.mp4"] == "ok" and tones["remux.mkv"] == "info" and tones["long.avi"] == "warn"
    assert by_id[server.videos["remux.mkv"]]["type"] == "MKV"
    for name in ("direct.mp4", "remux.mkv", "long.avi"):
        page.js(f"location.hash = '#/item/{server.videos[name]}'")
        page.wait_for("!!document.querySelector('.pill.mode')", message=f"{name}'s page")
        assert page.js(f"document.querySelector('.pill.mode').classList.contains('{tones[name]}')"), name


def test_back_to_a_long_list_returns_to_the_same_video(server, page):
    """Pages of one very tall card each, one per row (the test forces all three):
    scrolled four pages down, a video opened, then Back: the list loads those pages
    again before the scroll position is restored, so the same card is on screen
    (not the end of page 1)."""
    import time

    on_screen = """[...document.querySelectorAll('.grid.videos .card')]
        .filter(c => { const r = c.getBoundingClientRect(); return r.bottom > 0 && r.top < innerHeight; })
        .map(c => c.getAttribute('href'))"""
    page.goto(f"{server.base}/#/")
    page.wait_for("!!document.querySelector('.card')", message="home")
    page.js("""(() => {
        const real = window.fetch;
        window.fetch = (url, ...rest) => real(String(url).includes('/browse?') ? url + '&limit=1' : url, ...rest);
        document.head.append(Object.assign(document.createElement('style'), {textContent:
            '.grid.videos { grid-template-columns: 1fr !important } .grid.videos li { min-height: 3000px }'}));
        return true;
    })()""")
    page.js(f"location.hash = '#/library/{server.library}?all'")
    page.wait_for("document.querySelectorAll('.grid.videos li').length === 1", message="the first page")
    for _ in range(len(server.videos)):
        if page.js("document.querySelectorAll('.grid.videos li').length") >= 4:
            break
        page.js("window.scrollTo(0, document.documentElement.scrollHeight)")
        time.sleep(0.5)
    page.js("document.querySelectorAll('.grid.videos li')[3].scrollIntoView()")
    before, seen = page.js("window.scrollY"), page.js(on_screen)
    assert before > 3 * 3000 and len(seen) == 1
    page.js(f"location.hash = {seen[0]!r}")
    page.wait_for("!!document.querySelector('.detail')", message="the video's page")
    page.js("history.back()")
    page.wait_for(f"location.hash.endsWith('?all') && window.scrollY === {before}", message="back at the same spot")
    assert page.js(on_screen) == seen


def test_making_and_removing_an_mp4_copy(server, page):
    """Make MP4 copy on a video in the wrong container: progress, then the page plays
    the copy directly; Remove (after a question) plays the original again."""
    video = server.videos["remux.mkv"]
    page.goto(f"{server.base}/#/item/{video}")
    page.wait_for("!!document.querySelector('.pill.mode')", message="the video's page")
    assert page.js("document.querySelector('.pill.mode').textContent") == "Quick repackage"
    assert page.js("document.querySelector('.copy-section').hidden")
    button = "[...document.querySelectorAll('.detail-actions .btn')].find(b => b.textContent.includes('MP4'))"
    assert page.js(f"{button}.textContent") == "Make MP4 copy"
    page.js(f"{button}.click()")
    page.wait_for("!document.querySelector('.copy-section').hidden", timeout=60, message="the copy")
    assert page.js("document.querySelector('.pill.mode').textContent") == "Direct play"
    assert page.js("document.querySelector('.pill.copy').textContent") == "Copy"
    page.js(f"location.hash = '#/library/{server.library}'")                   # its card: MKV, plus COPY
    page.wait_for("!!document.querySelector('.type-badge.copy')", message="the card")
    assert page.js("document.querySelector('.type-badge.copy').closest('.meta').textContent").endswith("MKVCopy")
    page.js("history.back()")
    page.wait_for("!!document.querySelector('.copy-section') && !document.querySelector('.copy-section').hidden",
                  message="the video's page again")
    assert page.js(f"{button} === undefined || {button}.hidden")
    assert "Plays from an MP4 copy" in page.js("document.querySelector('.copy-note').textContent")
    assert server.call("GET", f"/api/items/{video}/plan")["delivery"] == "file"

    page.js("document.querySelector('.copy-section .text-btn').click()")
    page.wait_for("!!document.querySelector('dialog.confirm-dialog[open]')", message="the question")
    page.js("document.querySelector('dialog.confirm-dialog .btn.danger').click()")
    page.wait_for("document.querySelector('.copy-section').hidden && "
                  "document.querySelector('.pill.mode').textContent === 'Quick repackage'", message="the original again")
    assert server.call("GET", f"/api/items/{video}/plan")["delivery"] == "progressive"


def test_the_copies_page(server, page):
    """Copies from the top bar: a copy being made shows up with its progress, then
    in the list of copies; Remove (after a question) takes it off."""
    video = server.videos["remux.mkv"]
    page.goto(f"{server.base}/#/copies")
    page.wait_for("!!document.querySelector('.copies-made .empty')", message="the empty page")
    assert page.js("document.querySelector('.nav a.active').textContent") == "Copies"
    server.call("POST", f"/api/items/{video}/mp4-copy")
    page.js("location.hash = '#/'; location.hash = '#/copies'")
    page.wait_for("document.querySelectorAll('.copies-made .copy-row').length === 1", timeout=60, message="the copy")
    assert page.js("document.querySelector('.copies-made .copy-title').textContent") == "remux"
    assert "1 copy" in page.js("document.querySelector('.summary').textContent")
    page.js("document.querySelector('.copies-made .btn.danger').click()")
    page.wait_for("!!document.querySelector('dialog.confirm-dialog[open]')", message="the question")
    page.js("document.querySelector('dialog.confirm-dialog .btn.danger').click()")
    page.wait_for("!!document.querySelector('.copies-made .empty')", message="the empty page again")
    assert server.call("GET", f"/api/items/{video}")["copy_size"] is None


def test_making_english_subtitles(server, page):
    """Make English subtitles on a video's page (a stand-in for Whisper here): its
    progress on the button, then the finished subtitles on the page, with Download;
    and on the Subtitles page, with Remove."""
    import sys
    from pathlib import Path
    server.app.state.subtitles.worker = [sys.executable, str(Path(__file__).parent.parent / "fake_subtitle_worker.py")]
    video = server.videos["direct.mp4"]
    page.goto(f"{server.base}/#/item/{video}")
    button = "[...document.querySelectorAll('.detail-actions .btn')].find(b => /subtitles/i.test(b.textContent))"
    page.wait_for(f"!!({button})", message="the button")
    assert page.js(f"{button}.textContent") == "Make English subtitles"
    page.js(f"{button}.click()")
    page.wait_for("[...document.querySelectorAll('.copy-section h3')].some(h => h.textContent === 'English subtitles')",
                  message="the subtitles")
    assert "from Japanese" in page.js("[...document.querySelectorAll('.copy-note')].map(p => p.textContent).join()")
    assert page.js(f"!({button}) || {button}.hidden")
    assert "cake" in server.call("GET", f"/api/items/{video}/subtitles.vtt").decode()

    page.js("location.hash = '#/subtitles'")
    page.wait_for("document.querySelectorAll('.subtitles-made .copy-row').length === 1", message="the list")
    assert page.js("document.querySelector('.nav a.active').textContent") == "Subtitles"
    assert page.js("document.querySelector('.subtitles-made .copy-status').textContent").startswith("English from Japanese")
    page.js("document.querySelector('.subtitles-made .btn.danger').click()")
    page.wait_for("!!document.querySelector('dialog.confirm-dialog[open]')", message="the question")
    page.js("document.querySelector('dialog.confirm-dialog .btn.danger').click()")
    page.wait_for("!!document.querySelector('.subtitles-made .empty')", message="none left")
    assert server.call("GET", f"/api/items/{video}")["subtitles"] is None
