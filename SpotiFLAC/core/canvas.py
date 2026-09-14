"""Spotify Canvas — the 3-to-8 second looping visual a track carries.

What this is
------------
Canvas is the short vertical clip Spotify's mobile app loops behind the
player instead of the album cover. It is per *track*, it is silent, and it
is optional: most of the catalogue has none, and the ones that do are
usually a few hundred kilobytes of H.264.

Where it ends up
----------------
Beside the audio file, as its own sidecar — ``.mp4`` for the looping video
most canvases are, ``.jpg`` for the ones that are a still image; the
extension is read off the media URL (see :attr:`Canvas.suffix`). It is
deliberately **not** embedded: FLAC has no video stream to put it in, and
muxing it into an ``.m4a`` produces files that several players refuse to
open. A sidecar is
also the only shape anything else can consume — see the caveat below about
who actually reads one.

No media server picks these up on its own. Jellyfin's music scanner only
walks audio extensions, so the sidecar sits there ignored rather than
turning up as a stray video item; this is for archiving the track complete,
and for players that know to look. That is why the whole feature is off
unless asked for.

Where it comes from
-------------------
Two providers, tried in order:

``spotify``
    ``spclient``'s own ``canvaz-cache`` endpoint — a protobuf POST answered
    with a protobuf, on the same host and behind the same anonymous bearer
    token as the lyrics already use. No third party and no new dependency:
    the request message is three nested fields written by hand below, and
    the response goes through the generic walker in
    :mod:`SpotiFLAC.core.spotify_protobuf`.

``paxsenix``
    https://github.com/Paxsenix0/Spotify-Canvas-API, a JSON wrapper around
    that same endpoint. Kept as a fallback for when the direct call is
    blocked, in the same spirit as the Paxsenix lyrics providers this
    project already talks to.

Neither endpoint is documented by Spotify, and the Paxsenix project says as
much about its own terms of service. The switch is off by default.

A note on caching
-----------------
Hits are *not* cached. The CDN URL is short-lived, so a remembered one is a
404 by the time anyone reuses it — the only thing worth remembering is that
a track has no canvas at all, which is the common case and would otherwise
cost a request per track on every re-run of an album.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .http import NetworkManager
from .response_cache import get as get_cached_response
from .response_cache import put as put_cached_response
from .spotify_protobuf import read_fields

logger = logging.getLogger(__name__)

_CANVAZ = "https://spclient.wg.spotify.com/canvaz-cache/v0/canvases"
_PAXSENIX_CANVAS = "https://api.paxsenix.org/api/canvas"

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/145.0.0.0 Safari/537.36"

DEFAULT_CANVAS_PROVIDERS = ["spotify", "paxsenix"]

#: A track with no canvas is remembered, briefly. Long enough that
#: re-running a discography does not re-ask for every track, short enough
#: that a canvas added next week is still found this month.
_CANVAS_MISS_CACHE_TTL = 7 * 24 * 60 * 60

#: A canvas is a few hundred KB. Anything past this is not one, and a
#: sidecar has no business being larger than the track it sits next to.
MAX_CANVAS_BYTES = 32 * 1024 * 1024

#: Spotify IDs are 22 base62 characters. Tracks that came from a CSV row or
#: a local file carry something else in `id` — a filename, a UUID — and
#: there is no point asking spclient about those.
_TRACK_ID = re.compile(r"^[0-9A-Za-z]{22}$")

_VIDEO_SUFFIXES = (".mp4", ".m4v", ".webm", ".mov")
_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp")

#: Every extension a sidecar can end up with, for callers that need to ask
#: "is one of these already on disk?" before knowing which it would be.
CANVAS_SUFFIXES = (*_VIDEO_SUFFIXES, *_IMAGE_SUFFIXES)

#: Hosts a canvas may be fetched from. Neither provider's response is a
#: trusted source of URLs: the protobuf walker above takes *any* string
#: that starts with http, and the paxsenix walker takes any string under a
#: key that looks canvas-ish, out of JSON served by a third party. Without
#: this, whoever answers either endpoint chooses what this process
#: connects to — a link-local metadata address, an intranet host — and the
#: body comes back as a file on disk. Suffix-matched on the registrable
#: part so Spotify can move between CDN hostnames without a release here.
CANVAS_URL_HOSTS = (".scdn.co", ".spotifycdn.com")

#: The first bytes of the formats a canvas actually comes in. A 200 is not
#: proof of media: an error page, a JSON body, or an HTML interstitial all
#: arrive as one, and anything not rejected here is written to disk as a
#: sidecar and then trusted forever by the "already there" check in
#: downloader.py.
_MEDIA_SIGNATURES = (
    b"\x1aE\xdf\xa3",  # webm / matroska
    b"\xff\xd8\xff",  # jpeg
    b"\x89PNG\r\n\x1a\n",  # png
    b"GIF87a",
    b"GIF89a",
)


class CanvasUnavailable(Exception):
    """A provider could not answer — as opposed to answering "none".

    The difference matters because the answer is cached for a week. A
    provider that timed out, was rate-limited, or handed back something
    unreadable has said nothing about whether the track has a canvas, and
    remembering it as "no canvas" would suppress the whole feature for
    seven days over one bad minute. Only a provider that was actually
    asked and actually had nothing returns None.
    """


@dataclass(slots=True)
class Canvas:
    """A canvas that was found: where it lives and what kind it is."""

    url: str
    kind: str  # "video" or "image"
    provider: str

    @property
    def suffix(self) -> str:
        """The extension the sidecar should take.

        Read off the URL rather than off the protobuf's type enum: the
        enum distinguishes looping from non-looping video, which says
        nothing about the container, and the CDN path always carries the
        real extension (``…/video/<hash>.cnvs.mp4``).
        """
        path = self.url.split("?", 1)[0].lower()
        for suffix in (*_VIDEO_SUFFIXES, *_IMAGE_SUFFIXES):
            if path.endswith(suffix):
                return suffix
        return ".mp4" if self.kind == "video" else ".jpg"


def is_canvas_url(url: str) -> bool:
    """Whether `url` is one this process is willing to fetch.

    https only, and the host has to sit under one of CANVAS_URL_HOSTS —
    see that constant for why a URL read out of a provider response is not
    something to hand straight to an HTTP client.
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    # .hostname is already lower-cased and has any userinfo and port
    # stripped — `https://canvaz.scdn.co@evil.test/x` has hostname
    # "evil.test", which is the point of not splitting this by hand.
    host = parsed.hostname or ""
    return any(
        host == allowed.lstrip(".") or host.endswith(allowed)
        for allowed in CANVAS_URL_HOSTS
    )


def looks_like_canvas_media(payload: bytes) -> bool:
    """Whether a downloaded body is actually one of the canvas formats.

    mp4/m4v/mov carry their `ftyp` box at offset 4 rather than a fixed
    prefix, so it is checked separately from the signatures that do start
    at byte 0.
    """
    if len(payload) < 12:
        return False
    if payload[4:8] == b"ftyp":
        return True
    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return True
    return payload.startswith(_MEDIA_SIGNATURES)


def _kind_for(url: str) -> str:
    path = url.split("?", 1)[0].lower()
    if path.endswith(_IMAGE_SUFFIXES):
        return "image"
    return "video"


# ---------------------------------------------------------------------------
# Protobuf, by hand
# ---------------------------------------------------------------------------


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        chunk = value & 0x7F
        value >>= 7
        out.append(chunk | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _length_delimited(number: int, payload: bytes) -> bytes:
    """One length-delimited (wire type 2) field, tag included."""
    return _varint(number << 3 | 2) + _varint(len(payload)) + payload


def encode_canvaz_request(track_id: str) -> bytes:
    """``EntityCanvazRequest`` for a single track.

    The message is small enough to write out rather than depend on a
    protobuf runtime for::

        EntityCanvazRequest { repeated Entity entities = 1 }
        Entity { string entity_uri = 1 }
    """
    entity = _length_delimited(1, f"spotify:track:{track_id}".encode())
    return _length_delimited(1, entity)


def _urls_in(
    fields: dict[int, list[tuple[int, object]]],
    depth: int = 0,
) -> list[str]:
    """Every field of the message that holds an ``http(s)`` URL, in order.

    The canvas URL is field 2 of the Canvaz message, but nothing here
    depends on that staying true: the response is scanned for a string
    that is a URL, descending into nested messages as it goes, since the
    entries sit inside the repeated field. A renumbered field therefore
    costs nothing, which matters for an endpoint with no published schema.

    `depth` only guards against walking bytes that are not a message at
    all — random payloads parse as plausible-looking nested ones — and 4
    is well past anything this response nests to.
    """
    found: list[str] = []
    if depth > 4:
        return found
    for entries in fields.values():
        for wire_type, value in entries:
            if wire_type != 2 or not isinstance(value, (bytes, bytearray)):
                continue
            text = bytes(value).decode("utf-8", errors="replace")
            if text.startswith(("http://", "https://")):
                found.append(text)
            elif len(value) > 2:
                found.extend(_urls_in(read_fields(bytes(value)), depth + 1))
    return found


def parse_canvaz_response(raw: bytes) -> Canvas | None:
    """The first canvas in an ``EntityCanvazResponse``, or None.

    The response carries the artist's avatar as well as the canvas, so a
    URL that names the canvas CDN is taken ahead of field order; the
    plain first URL is the fallback for when the CDN is renamed.
    """
    try:
        urls = _urls_in(read_fields(raw))
    except Exception as exc:  # a truncated body must not fail a download
        logger.debug("[canvas] could not read the protobuf: %s", exc)
        return None
    if not urls:
        return None

    preferred = next(
        (url for url in urls if "canvaz" in url.lower() or "cnvs" in url.lower()),
        urls[0],
    )
    return Canvas(url=preferred, kind=_kind_for(preferred), provider="spotify")


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


async def _fetch_spotify(track_id: str, timeout: int) -> Canvas | None:
    # Imported here, and deliberately the lyrics module's own helper: it
    # holds the TOTP dance and a shared token cache, and a second copy
    # would mean two tokens and two refreshes for one run.
    from .lyrics import _get_spotify_anon_token, _invalidate_spotify_token

    token = await _get_spotify_anon_token(timeout)
    if not token:
        raise CanvasUnavailable("no anonymous token")

    client = await NetworkManager.get_async_client_safe()
    body = encode_canvaz_request(track_id)

    def headers(bearer: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {bearer}",
            "Accept": "application/protobuf",
            "Content-Type": "application/protobuf",
            "App-Platform": "WebPlayer",
            "User-Agent": _UA,
        }

    response = await client.post(
        _CANVAZ, content=body, headers=headers(token), timeout=timeout
    )
    if response.status_code == 401:
        await _invalidate_spotify_token()
        token = await _get_spotify_anon_token(timeout)
        if not token:
            return None
        response = await client.post(
            _CANVAZ, content=body, headers=headers(token), timeout=timeout
        )

    if response.status_code != 200:
        # Not "this track has no canvas" — spclient says that with a 200
        # and an empty body. A 429 or a 503 is the endpoint having a
        # moment, and must not be cached as an absence.
        raise CanvasUnavailable(f"HTTP {response.status_code} for {track_id}")

    # An empty 200 is the honest answer for "this track has no canvas",
    # and it is by far the most common one.
    return parse_canvaz_response(response.content)


def _url_in_json(data: object) -> str:
    """The first canvas URL anywhere in a JSON payload.

    Same reasoning as `_urls_in`: the wrapper's response shape is not a
    contract, so the value is looked for by what it is rather than by
    where it sits.
    """
    if isinstance(data, str):
        return data if data.startswith(("http://", "https://")) else ""
    if isinstance(data, dict):
        # A key that names the canvas wins over any other URL in the
        # payload — the response also carries artist avatars.
        for key, value in data.items():
            if "canvas" in str(key).lower() and "url" in str(key).lower():
                found = _url_in_json(value)
                if found:
                    return found
        for value in data.values():
            found = _url_in_json(value)
            if found:
                return found
        return ""
    if isinstance(data, list):
        for item in data:
            found = _url_in_json(item)
            if found:
                return found
    return ""


async def _fetch_paxsenix(track_id: str, timeout: int) -> Canvas | None:
    client = await NetworkManager.get_async_client_safe()
    response = await client.get(
        _PAXSENIX_CANVAS,
        params={"trackId": track_id},
        headers={"User-Agent": _UA, "Accept": "application/json"},
        timeout=timeout,
    )
    if response.status_code != 200:
        raise CanvasUnavailable(f"HTTP {response.status_code} for {track_id}")

    # A body that is not JSON is the wrapper being down or captive-portalled,
    # not an answer: let the raise reach fetch_canvas_async as a failure.
    url = _url_in_json(response.json())
    if not url:
        return None
    return Canvas(url=url, kind=_kind_for(url), provider="paxsenix")


_PROVIDERS = {
    "spotify": _fetch_spotify,
    "paxsenix": _fetch_paxsenix,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def fetch_canvas_async(
    track_id: str,
    *,
    providers: list[str] | None = None,
    timeout: int = 7,
) -> Canvas | None:
    """The track's canvas, or None when it has none.

    Never raises: a canvas is a nicety on top of a download that has
    already succeeded, so every failure here is a debug line and a None.
    """
    if not track_id or not _TRACK_ID.match(track_id):
        return None

    order = [p for p in (providers or DEFAULT_CANVAS_PROVIDERS) if p in _PROVIDERS]
    if not order:
        return None

    cache_key = f"{track_id}|{','.join(order)}"
    if get_cached_response("canvas-miss", cache_key, _CANVAS_MISS_CACHE_TTL):
        return None

    # Only a run in which every provider was reachable and none had a
    # canvas is worth remembering — see CanvasUnavailable.
    absent = True
    for name in order:
        try:
            canvas = await _PROVIDERS[name](track_id, timeout)
        except CanvasUnavailable as exc:
            logger.debug("[canvas/%s] unavailable: %s", name, exc)
            absent = False
            continue
        except Exception as exc:
            # A provider that raised something it did not mean to is a
            # failure too: it did not get as far as an answer either.
            logger.debug("[canvas/%s] %s", name, exc)
            absent = False
            continue
        if canvas and canvas.url:
            return canvas

    if absent:
        put_cached_response("canvas-miss", cache_key, True)
    return None


async def download_canvas_async(canvas: Canvas, *, timeout: int = 20) -> bytes | None:
    """The canvas itself.

    Returned as bytes rather than written here: the same clip can have to
    land in two places (beside the track and in a canvas library), and
    fetching a signed CDN URL twice is both slower and a second chance to
    fail.
    """
    if not is_canvas_url(canvas.url):
        # Rejected rather than requested: the URL came out of a provider
        # response, and a request is itself the damage for an address this
        # process should not be reaching. See CANVAS_URL_HOSTS.
        logger.debug("[canvas] refusing to fetch %s", canvas.url)
        return None

    try:
        client = await NetworkManager.get_async_client_safe()
        async with client.stream(
            "GET",
            canvas.url,
            headers={"User-Agent": _UA},
            timeout=timeout,
            # A redirect is a second URL, and this one would not have gone
            # through is_canvas_url. httpx already defaults to not
            # following them; stated here so a change to the shared client
            # cannot quietly reopen the hole the check above closes.
            follow_redirects=False,
        ) as response:
            if response.status_code != 200:
                logger.debug(
                    "[canvas] %s downloading %s", response.status_code, canvas.url
                )
                return None
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > MAX_CANVAS_BYTES:
                    logger.debug(
                        "[canvas] %s is larger than a canvas should be", canvas.url
                    )
                    return None

        payload = bytes(chunks)
        if not payload:
            return None
        # A 200 is not proof of media. An error page, a JSON `{"error":…}`
        # or an HTML interstitial all arrive as one, and the caller writes
        # whatever comes back to disk as a sidecar that the "already
        # there" check then trusts on every future run.
        if not looks_like_canvas_media(payload):
            logger.debug("[canvas] %s did not return canvas media", canvas.url)
            return None
        return payload
    except Exception as exc:
        logger.debug("[canvas] download failed: %s", exc)
        return None
