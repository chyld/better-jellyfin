"""Downloading an image from a URL (for tag, folder and video pictures).

The server does the fetching, so each address is checked first, and the
connection then goes to exactly the address that was checked. (Resolving the
name again when connecting would let a DNS server answer differently the second
time, "DNS rebinding", and reach a private address after passing the check.)

Which addresses are allowed is an explicit policy (REEL_IMAGE_URLS):

    internet  public addresses only (default)
    lan       public addresses and the local network (192.168.x.x, 10.x.x.x, ...)
    off       no URL downloads at all

This machine (localhost), link-local addresses such as cloud metadata at
169.254.169.254, multicast and reserved addresses are always refused, on every
redirect too.
"""
import http.client
import ipaddress
import socket
import ssl
import time
from urllib.parse import urljoin, urlsplit

from .images import MAX_UPLOAD_BYTES

TIMEOUT = 15  # seconds, for the whole download
MAX_REDIRECTS = 5
USER_AGENT = "Reel/0.1 (+self-hosted media server)"
POLICIES = ("internet", "lan", "off")
DEFAULT_POLICY = "internet"


class FetchError(Exception):
    """The URL couldn't be used; the message is shown to the user."""


def address_allowed(ip: str, policy: str) -> bool:
    """May the server connect to this address under the policy?"""
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    if addr.is_loopback or addr.is_link_local or addr.is_unspecified or addr.is_multicast or addr.is_reserved:
        return False
    if policy == "lan":
        return True
    return addr.is_global


def resolve(host: str, port: int, policy: str) -> str:
    """Look the host up once, check every address, and return the one to use."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        raise FetchError(f"Couldn't find the host {host!r}.")
    addresses = list(dict.fromkeys(info[4][0] for info in infos))
    # Refuse if *any* answer is off-limits, rather than picking a "good" one.
    if not addresses or not all(address_allowed(a, policy) for a in addresses):
        if policy == "lan":
            raise FetchError("Images can't be fetched from this server itself or link-local addresses.")
        raise FetchError(
            "Images can only be fetched from public internet addresses "
            "(set REEL_IMAGE_URLS=lan to allow your local network)."
        )
    return addresses[0]


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Connects to a given IP address, while the request still names the host."""

    def __init__(self, host: str, port: int, ip: str, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Like _PinnedHTTPConnection, with TLS verified against the host name."""

    def __init__(self, host: str, port: int, ip: str, timeout: float):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def fetch_image_bytes(url: str, *, policy: str = DEFAULT_POLICY) -> bytes:
    """Download up to MAX_UPLOAD_BYTES from `url`. Raises FetchError."""
    if policy == "off":
        raise FetchError("Downloading images from URLs is turned off on this server (REEL_IMAGE_URLS=off).")
    url = (url or "").strip()
    if not url:
        raise FetchError("Paste the web address of an image.")
    deadline = time.monotonic() + TIMEOUT

    for _ in range(MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise FetchError("Use a web address starting with http:// or https://.")
        if not parts.hostname:
            raise FetchError("That web address has no host name.")
        try:
            port = parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError:
            raise FetchError("That web address has an invalid port.")
        ip = resolve(parts.hostname, port, policy)

        connection_class = _PinnedHTTPSConnection if parts.scheme == "https" else _PinnedHTTPConnection
        conn = connection_class(parts.hostname, port, ip, timeout=max(1.0, deadline - time.monotonic()))
        try:
            target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
            conn.request("GET", target, headers={"User-Agent": USER_AGENT, "Accept": "image/*"})
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location:
                    raise FetchError(f"The site answered {response.status} without saying where to go.")
                url = urljoin(url, location)  # checked again at the top of the loop
                continue
            if response.status != 200:
                raise FetchError(f"The site answered {response.status} ({response.reason}).")
            # Refuse before downloading what clearly isn't a picture. (Not "must be
            # image/*": some servers label real pictures application/octet-stream;
            # the picture itself is checked once downloaded.)
            kind = (response.getheader("Content-Type") or "").split(";")[0].strip().lower()
            if kind.startswith("text/") or kind in ("application/json", "application/xml", "application/xhtml+xml"):
                raise FetchError("That address is a web page, not a picture. Copy the picture's own address "
                                 "(right-click it, \"Copy image address\").")
            length = response.getheader("Content-Length")
            if length and length.isdigit() and int(length) > MAX_UPLOAD_BYTES:
                raise FetchError(f"Images can be at most {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
            data = bytearray()
            while len(data) <= MAX_UPLOAD_BYTES:
                if time.monotonic() > deadline:
                    raise FetchError(f"The download took longer than {TIMEOUT} seconds.")
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                data += chunk
        except FetchError:
            raise
        except ssl.SSLError as exc:
            raise FetchError(f"Couldn't make a secure connection: {exc.reason or exc}.")
        except (OSError, http.client.HTTPException) as exc:
            raise FetchError(f"Couldn't download the image: {exc}.")
        finally:
            conn.close()

        if len(data) > MAX_UPLOAD_BYTES:
            raise FetchError(f"Images can be at most {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
        if not data:
            raise FetchError("That address returned nothing.")
        return bytes(data)

    raise FetchError("That web address redirects too many times.")
