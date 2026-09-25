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
