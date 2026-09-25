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
