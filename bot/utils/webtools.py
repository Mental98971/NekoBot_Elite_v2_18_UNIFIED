"""Small, dependency-free helpers for plugins that pass user-supplied URLs or text to outside services.

Standard library only (importable and unit-testable without the Telegram stack).  Everything here is a pure
function: no network, no clock, no global state.

* ``normalize_url`` / ``is_public_host`` - accept only public http(s) URLs (no credentials, no local or private
  addresses, no numeric tricks), so ``/webss`` cannot be pointed at internal-looking targets.
* ``safe_href`` / ``ddg_search_url`` - values that are safe inside an HTML ``href="..."`` attribute.
* ``stable_seed`` - a process-independent replacement for ``hash()`` (which Python randomises per process).
* ``parse_coordinates`` - "35.65, 139.74" style input.
* ``clip`` - length limiting that never splits a surrogate pair or leaves a dangling HTML entity.
"""
from __future__ import annotations

import html
import ipaddress
import re
import zlib
from typing import Optional, Tuple
from urllib.parse import quote_plus, urlsplit

MAX_URL_LENGTH = 2048

_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")
_ANY_SCHEME_LIKE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:(?!\d+(?:/|$))")  # "javascript:...", "mailto:..." but not "host:8080"
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_TLD = re.compile(r"^(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$")
_BLOCKED_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".corp", ".intranet", ".private", ".test", ".invalid", ".example")
_BLOCKED_HOSTS = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "broadcasthost", "metadata.google.internal"})
_COORD = re.compile(r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*[,;\s]\s*(-?\d{1,3}(?:\.\d+)?)\s*$")


def _ip_is_public(ip: "ipaddress._BaseAddress") -> bool:
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return bool(ip.is_global) and not (ip.is_multicast or ip.is_unspecified or ip.is_loopback or ip.is_link_local or ip.is_private or ip.is_reserved)


def is_public_host(host: Optional[str]) -> bool:
    """True for a hostname that looks like a real public DNS name or a globally routable IP literal."""
    if not host:
        return False
    h = host.strip().strip(".").lower()
    if h and not h.isascii():  # internationalised name: judge its punycode form
        try:
            h = h.encode("idna").decode("ascii")
        except UnicodeError:
            return False
    if not h or len(h) > 253:
        return False
    try:  # IP literal (v4, or v6 without brackets as urlsplit().hostname returns it)
        return _ip_is_public(ipaddress.ip_address(h))
    except ValueError:
        pass
    if h in _BLOCKED_HOSTS or h.endswith(_BLOCKED_SUFFIXES):
        return False
    labels = h.split(".")
    if len(labels) < 2:  # single-label names ("intranet", "2130706433") resolve inside local networks
        return False
    if not all(_LABEL.match(lab) for lab in labels):
        return False
    return bool(_TLD.match(labels[-1]))  # numeric / hex "TLDs" (0x7f.0.0.1, 127.1) are rejected


def normalize_url(raw: Optional[str]) -> Optional[str]:
    """``example.com/a`` -> ``https://example.com/a``.  None unless the result is a public http(s) URL without credentials."""
    s = (raw or "").strip().strip("<>").strip()
    if not s or len(s) > MAX_URL_LENGTH:
        return None
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in s):
        return None
    if _SCHEME.match(s):
        pass
    elif _ANY_SCHEME_LIKE.match(s) or s.startswith("//"):
        return None  # javascript:, data:, mailto:, protocol-relative ...
    else:
        s = "https://" + s
    try:
        parts = urlsplit(s)
        host = parts.hostname
        _ = parts.port  # raises ValueError for a malformed or out-of-range port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or parts.username is not None or parts.password is not None:
        return None
    if not is_public_host(host):
        return None
    return s


def safe_href(url: Optional[str]) -> str:
    """Value for ``<a href="...">`` (HTML-escaped incl. quotes); '' if ``url`` is not an http(s) URL."""
    u = (url or "").strip()
    if not re.match(r"^https?://[^\s]+$", u, re.IGNORECASE):
        return ""
    return html.escape(u, quote=True)


def ddg_search_url(query: str) -> str:
    return "https://duckduckgo.com/?q=" + quote_plus((query or "").strip()[:300])


def stable_seed(text: str, modulo: int = 10000) -> int:
    """Deterministic across processes and platforms (``hash()`` is salted per process)."""
    if modulo < 1:
        raise ValueError("modulo must be >= 1")
    return zlib.crc32((text or "").casefold().encode("utf-8")) % modulo


def parse_coordinates(text: Optional[str]) -> Optional[Tuple[float, float]]:
    """``"35.65, 139.74"`` -> (35.65, 139.74); None when it is not a coordinate pair or is out of range."""
    m = _COORD.match(text or "")
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return lat, lon


def clip(text: Optional[str], limit: int, suffix: str = "\u2026") -> str:
    """Shorten to at most ``limit`` characters (suffix included) on a word boundary where possible."""
    t = " ".join((text or "").split())
    if limit <= 0:
        return ""
    if len(t) <= limit:
        return t
    cut = max(0, limit - len(suffix))
    head = t[:cut]
    if " " in head and len(head) - head.rfind(" ") < 20:
        head = head[: head.rfind(" ")]
    return head.rstrip() + suffix
