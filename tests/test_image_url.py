"""Setting a tag's image from a URL: the server downloads it."""
import http.server
import socket
import subprocess
import threading
from urllib.parse import urlsplit

import pytest

from reel import fetch
from reel.images import MAX_UPLOAD_BYTES

from conftest import make_files, requires_ffmpeg


# ---- Which addresses may be fetched (no network needed) --------------------------------


@pytest.mark.parametrize(
    "ip", ["127.0.0.1", "::1", "169.254.169.254", "0.0.0.0", "::ffff:127.0.0.1", "224.0.0.1", "240.0.0.1"],
)
@pytest.mark.parametrize("policy", ["internet", "lan"])
def test_always_refused(ip, policy):
    assert fetch.address_allowed(ip, policy) is False


@pytest.mark.parametrize("ip", ["192.168.1.10", "10.0.0.5", "172.16.3.4", "fd00::1", "100.64.0.1"])
def test_local_network_needs_the_lan_policy(ip):
    assert fetch.address_allowed(ip, "internet") is False
    assert fetch.address_allowed(ip, "lan") is True


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])
def test_public_addresses_are_allowed(ip):
    assert fetch.address_allowed(ip, "internet") is True
    assert fetch.address_allowed(ip, "lan") is True


def test_a_name_with_any_bad_answer_is_refused(monkeypatch):
    """If DNS returns a public and a private address, refuse rather than pick one."""
    answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 80)) for ip in ("8.8.8.8", "127.0.0.1")]
    monkeypatch.setattr(fetch.socket, "getaddrinfo", lambda *a, **k: answers)
    with pytest.raises(fetch.FetchError):
        fetch.resolve("mixed.test", 80, "internet")


@pytest.mark.parametrize("url", ["ftp://example.com/a.jpg", "file:///etc/passwd", "javascript:alert(1)", "example.com/a.jpg"])
def test_only_http_and_https(url):
    with pytest.raises(fetch.FetchError, match="http:// or https://"):
        fetch.fetch_image_bytes(url)


def test_empty_url():
    with pytest.raises(fetch.FetchError, match="Paste"):
        fetch.fetch_image_bytes("   ")


def test_turned_off():
    with pytest.raises(fetch.FetchError, match="turned off"):
        fetch.fetch_image_bytes("https://example.com/a.jpg", policy="off")


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

    hosts_seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hosts_seen.append(self.headers.get("Host"))
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
    site = f"http://127.0.0.1:{server.server_port}"
    global site_hosts_seen
    site_hosts_seen = hosts_seen
    yield site
    server.shutdown()


@pytest.fixture
def allow_test_server(monkeypatch):
    """Let the server fetch from the local test site (127.0.0.1); everything else as usual."""
    real = fetch.address_allowed
    monkeypatch.setattr(fetch, "address_allowed", lambda ip, policy: ip == "127.0.0.1" or real(ip, policy))


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
        ("/to-metadata", "public internet addresses"),   # a redirect can't sneak past the check
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
    assert "public internet addresses" in res.json()["detail"]


@requires_ffmpeg
def test_dns_rebinding_cannot_change_the_address(client, tag_id, site, allow_test_server, monkeypatch):
    """The host is looked up once, checked, and then connected to by that exact address.

    rebind.test only resolves through this test's resolver, to the test site. If
    the download resolved the name a second time (as urllib did when connecting),
    it would fail; instead it connects to the checked address and still sends
    the real host name.
    """
    port = int(site.rsplit(":", 1)[1])
    lookups = []
    real_getaddrinfo = socket.getaddrinfo

    def resolver(host, *args, **kwargs):
        if host == "127.0.0.1":  # connecting to a literal IP: not a DNS lookup
            return real_getaddrinfo(host, *args, **kwargs)
        lookups.append(host)
        assert host == "rebind.test", f"unexpected lookup of {host}"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(fetch.socket, "getaddrinfo", resolver)
    res = set_from_url(client, tag_id, f"http://rebind.test:{port}/pic.png")
    assert res.status_code == 200, res.text
    assert lookups == ["rebind.test"]                    # exactly one lookup
    assert site_hosts_seen[-1] == f"rebind.test:{port}"  # and the Host header is the name


def test_unknown_tag(client):
    assert set_from_url(client, "nope", "https://example.com/a.jpg").status_code == 404
