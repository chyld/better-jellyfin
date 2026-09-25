"""Setting a tag's image from a URL: the server downloads it."""
import http.server
import subprocess
import threading
from urllib.parse import urlsplit

import pytest

from reel import fetch
from reel.images import MAX_UPLOAD_BYTES

from conftest import make_files, requires_ffmpeg


# ---- Which addresses may be fetched (no network needed) --------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/pic.jpg", "http://localhost/pic.jpg", "http://[::1]/pic.jpg",
        "http://169.254.169.254/latest/meta-data", "http://0.0.0.0/pic.jpg",
        "http://[::ffff:127.0.0.1]/pic.jpg",
    ],
)
def test_this_machine_and_link_local_are_blocked(url):
    with pytest.raises(fetch.FetchError, match="this server itself or link-local"):
        fetch.check_url(url)


@pytest.mark.parametrize("url", ["ftp://example.com/a.jpg", "file:///etc/passwd", "javascript:alert(1)", "example.com/a.jpg"])
def test_only_http_and_https(url):
    with pytest.raises(fetch.FetchError, match="http:// or https://"):
        fetch.check_url(url)


@pytest.mark.parametrize("url", ["http://192.168.1.10/a.jpg", "https://10.0.0.5:8443/a.png", "http://8.8.8.8/a.jpg"])
def test_lan_and_internet_addresses_are_allowed(url):
    fetch.check_url(url)  # no error


def test_empty_url():
    with pytest.raises(fetch.FetchError, match="Paste"):
        fetch.fetch_image_bytes("   ")


# ---- Downloading from a local test web server ------------------------------------------


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    """A small web server with a picture, a non-image, a huge file and redirects."""
    folder = tmp_path_factory.mktemp("site")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=teal:s=1200x600",
                    "-frames:v", "1", str(folder / "pic.png")], check=True)
    pages = {
        "/pic.png": (200, (folder / "pic.png").read_bytes()),
        "/page.html": (200, b"<html>not a picture</html>"),
        "/huge.png": (200, b"\x89PNG\r\n\x1a\n" + b"\0" * MAX_UPLOAD_BYTES),
        "/empty.png": (200, b""),
    }

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/to-pic":
                self.send_response(302)
                self.send_header("Location", "/pic.png")
                self.end_headers()
                return
            if self.path == "/to-metadata":
                self.send_response(302)
                self.send_header("Location", "http://169.254.169.254/latest/meta-data")
                self.end_headers()
                return
            if self.path == "/loop":
                self.send_response(302)
                self.send_header("Location", "/loop")
                self.end_headers()
                return
            status, body = pages.get(self.path, (404, b"missing"))
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
def allow_test_server(monkeypatch, site):
    """Let the server fetch from the local test site; everything else is checked as usual."""
    real_check = fetch.check_url
    test_host = urlsplit(site).netloc

    def check(url):
        if urlsplit(url).netloc != test_host:
            real_check(url)

    monkeypatch.setattr(fetch, "check_url", check)


@pytest.fixture
def tag_id(client, media_root):
    make_files(media_root, "Tapes/a.mpg", "Tapes/b.mpg")
    lib = client.post("/api/libraries", json={"name": "Tapes", "path": str(media_root / "Tapes")}).json()
    client.post(f"/api/libraries/{lib['id']}/scan")
    client.scans.wait_idle()
    item = client.get(f"/api/libraries/{lib['id']}/browse").json()["items"][0]
    return client.post(f"/api/items/{item['id']}/tags", json={"name": "family"}).json()[0]["id"]


def set_from_url(client, tag_id, url):
    return client.post(f"/api/tags/{tag_id}/image-url", json={"url": url})


@requires_ffmpeg
def test_image_from_url(client, tag_id, site, allow_test_server):
    res = set_from_url(client, tag_id, f"{site}/pic.png")
    assert res.status_code == 200, res.text
    assert res.json()["image"]
    img = client.get(f"/api/tags/{tag_id}/image")
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"


@requires_ffmpeg
def test_redirects_are_followed(client, tag_id, site, allow_test_server):
    assert set_from_url(client, tag_id, f"{site}/to-pic").status_code == 200


@requires_ffmpeg
@pytest.mark.parametrize(
    "path, message",
    [
        ("/page.html", "isn't a JPG"),
        ("/nope.png", "answered 404"),
        ("/huge.png", "at most 20 MB"),
        ("/empty.png", "returned nothing"),
        ("/to-metadata", "link-local"),   # a redirect can't sneak past the check
        ("/loop", "redirects too many times"),
    ],
)
def test_bad_urls_are_explained(client, tag_id, site, allow_test_server, path, message):
    res = set_from_url(client, tag_id, f"{site}{path}")
    assert res.status_code == 400
    assert message in res.json()["detail"]
    assert client.get(f"/api/tags/{tag_id}/image").status_code == 404  # nothing was set


def test_this_server_is_refused_without_the_test_allowance(client, tag_id, site):
    res = set_from_url(client, tag_id, f"{site}/pic.png")  # the test site is on 127.0.0.1
    assert res.status_code == 400
    assert "this server itself" in res.json()["detail"]


def test_unknown_tag(client):
    assert set_from_url(client, "nope", "https://example.com/a.jpg").status_code == 404
