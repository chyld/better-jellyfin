"""Downloading an image from a URL (for tag images).

The server does the fetching, so the address is checked first: only http(s),
and never this machine or link-local addresses (such as cloud metadata at
169.254.169.254), including after redirects. Other machines on the local
network are allowed, so pictures from another homelab server work.
"""
import ipaddress
import socket
import urllib.error
import urllib.request
from collections.abc import Callable
from urllib.parse import urlsplit

from .images import MAX_UPLOAD_BYTES

TIMEOUT = 15  # seconds
MAX_REDIRECTS = 5
USER_AGENT = "Reel/0.1 (+self-hosted media server)"


class FetchError(Exception):
    """The URL couldn't be used; the message is shown to the user."""


def _blocked_address(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_loopback or addr.is_link_local or addr.is_unspecified or addr.is_multicast


def check_url(url: str) -> None:
    """Refuse URLs the server shouldn't fetch. Raises FetchError."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise FetchError("Use a web address starting with http:// or https://.")
    if not parts.hostname:
        raise FetchError("That web address has no host name.")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except (socket.gaierror, UnicodeError):
        raise FetchError(f"Couldn't find the host {parts.hostname!r}.")
    if any(_blocked_address(info[4][0]) for info in infos):
        raise FetchError("Images can't be fetched from this server itself or link-local addresses.")


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to addresses that pass the same check."""

    def __init__(self, check: Callable[[str], None]):
        self.check = check
        self.count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.count += 1
        if self.count > MAX_REDIRECTS:
            raise FetchError("That web address redirects too many times.")
        self.check(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_image_bytes(url: str, *, check: Callable[[str], None] | None = None) -> bytes:
    """Download up to MAX_UPLOAD_BYTES from `url`. Raises FetchError."""
    check = check or check_url
    url = (url or "").strip()
    if not url:
        raise FetchError("Paste the web address of an image.")
    check(url)
    opener = urllib.request.build_opener(_CheckedRedirects(check))
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "image/*"})
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            data = response.read(MAX_UPLOAD_BYTES + 1)
    except FetchError:
        raise
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:  # urllib gave up on a redirect loop
            raise FetchError("That web address redirects too many times.")
        raise FetchError(f"The site answered {exc.code} ({exc.reason}).")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        raise FetchError(f"Couldn't download the image: {reason}.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise FetchError(f"Images can be at most {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    if not data:
        raise FetchError("That address returned nothing.")
    return data
