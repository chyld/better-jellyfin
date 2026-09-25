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
