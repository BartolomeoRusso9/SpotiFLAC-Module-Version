"""Shared, safe URL-host matching helpers.

`"spotify.com" in url` (a bare substring check) is the classic
incomplete-URL-sanitization bug: it also matches
``https://evil.com/spotify.com``, ``https://spotify.com.evil.com/`` or
``https://notspotify.com/``. Every call site in this codebase that needs to
recognize "is this URL from provider X" should go through
:func:`url_host_matches` instead, which parses the URL and compares the
actual hostname (exact match or a proper ``.``-bounded subdomain).
"""

from __future__ import annotations

from urllib.parse import urlparse


def url_host_matches(url: str, *domains: str) -> bool:
    """Return True if ``url``'s hostname is exactly one of ``domains`` or a
    subdomain of one of them.

    Comparison is case-insensitive. Unlike a plain substring check, this
    can't be fooled by the domain appearing in the path, query string, or as
    a suffix/prefix of an unrelated host (e.g. ``notspotify.com`` or
    ``spotify.com.evil.com``).
    """
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    for domain in domains:
        domain = domain.lower().strip(".")
        if not domain:
            continue
        if host == domain or host.endswith("." + domain):
            return True
    return False


def url_host_has_label(url: str, *labels: str) -> bool:
    """Return True if any of ``labels`` appears as a whole, dot-delimited
    component of the URL's hostname (e.g. label ``"amazon"`` matches
    ``amazon.com``, ``music.amazon.de``, ``amazon.co.uk`` — but not
    ``evilamazon.com`` or ``amazonclone.net``).

    Useful for domains with many country-specific TLDs where enumerating
    every ``amazon.<tld>`` combination in :func:`url_host_matches` would be
    both incomplete and unwieldy.
    """
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    parts = set(host.split("."))
    return any(label.lower() in parts for label in labels)


def url_path_contains(url: str, *segments: str) -> bool:
    """Return True if the URL's *path* contains any of ``segments`` as an
    exact path component (e.g. ``listen.tidal.com/track/123`` matches
    segment ``"track"``, but ``/track-preview`` or ``/tracklist`` do not) —
    used for things like ``listen.tidal.com/track`` where the check is
    really "host + path prefix" together. Prefer :func:`url_host_matches`
    when only the host matters.
    """
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    parts = [p.lower() for p in parsed.path.split("/") if p]
    for segment in segments:
        segment = segment.lower().strip("/")
        if not segment:
            continue
        seg_parts = segment.split("/")
        if segment in host:
            return True
        if any(
            parts[i : i + len(seg_parts)] == seg_parts
            for i in range(len(parts) - len(seg_parts) + 1)
        ):
            return True
    return False
