"""metadata_fallback.py — When Spotify's or Apple Music's own client fails.

The built-in Spotify client depends on things Spotify changes without
notice: its web-player secrets, the TOTP that turns them into a token, the
GraphQL hashes. When one of them moves, every Spotify link stops resolving
until the client is fixed. The mobile app's `spotify-web` and `apple-music`
extensions read the same catalogues by their own route and are maintained
separately, so the day one breaks is rarely the day the other does.

So the built-in client stays first, always — it is faster, and it carries
what the extensions do not (play counts, composer, Canvas, TTML lyrics).
Only when it raises, or finds nothing, is an installed metadata extension
whose `urlHandler` claims the link asked instead, and the log says so.

A track resolved this way gets the id the built-in client would have given
it — the bare Spotify id, `apple_<id>` for Apple Music — so the ISRC lookup,
duplicate checks and the download log see the same track either way.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from .errors import ErrorKind, SpotiflacError
from .extension_metadata import _first_image, _int, _text
from .models import TrackMetadata
from .url_utils import url_host_matches

logger = logging.getLogger(__name__)

#: Services whose built-in client an extension can stand in for, with the id
#: prefix that client gives its tracks.
_ID_PREFIX = {"spotify": "", "apple": "apple_"}

_LABELS = {"spotify": "Spotify", "apple": "Apple Music"}

#: How long one extension call may take.
EXTENSION_TIMEOUT_S = 90


def native_service_for(url: str) -> str | None:
    if (url or "").startswith("spotify:") or url_host_matches(
        url, "open.spotify.com", "spotify.com"
    ):
        return "spotify"
    if url_host_matches(url, "music.apple.com"):
        return "apple"
    return None


def fallback_extension_for(url: str, manager: Any = None) -> str | None:
    """The installed metadata extension whose urlHandler claims `url`."""
    if manager is None:
        from ..extensions.manager import ExtensionManager

        manager = ExtensionManager(auto_install_downloads=False)
    try:
        installed = manager.list_installed()
    except Exception as exc:
        logger.debug("[metadata] could not list extensions: %s", exc)
        return None
    lowered = (url or "").lower()
    for ext in installed:
        if "metadata_provider" not in (ext.types or []):
            continue
        handler = ext.manifest.get("urlHandler")
        if isinstance(handler, dict) and handler.get("enabled") is False:
            continue
        if any(str(p).lower() in lowered for p in ext.url_patterns if p):
            return ext.name
    return None


def _external_url(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("spotify", "apple", "appleMusic"):
            if value.get(key):
                return str(value[key])
        return str(next((v for v in value.values() if v), ""))
    return str(value or "")


def track_from_extension(
    item: dict, service: str, collection: dict | None = None
) -> TrackMetadata | None:
    """One track as spotify-web or apple-music describe it."""
    if not isinstance(item, dict):
        return None
    raw_id = str(item.get("spotify_id") or item.get("id") or "")
    title = _text(item.get("name") or item.get("title"))
    if not raw_id or not title:
        return None

    prefix = _ID_PREFIX[service]
    collection = collection or {}
    artists = _text(item.get("artists") or item.get("artist"))
    album = _text(item.get("album_name"))
    if not album and collection.get("type") == "album":
        album = _text(collection.get("name"))

    return TrackMetadata(
        id=raw_id if raw_id.startswith(prefix) else f"{prefix}{raw_id}",
        title=title,
        artists=artists,
        album=album,
        album_artist=_text(item.get("album_artist")) or artists,
        isrc=str(item.get("isrc") or "").strip().upper(),
        track_number=_int(item.get("track_number")),
        disc_number=_int(item.get("disc_number")) or 1,
        total_tracks=_int(item.get("total_tracks")),
        total_discs=_int(item.get("total_discs")) or 1,
        duration_ms=_int(item.get("duration_ms")),
        release_date=str(item.get("release_date") or ""),
        cover_url=_first_image(item) or _first_image(collection),
        external_url=_external_url(
            item.get("external_urls") or item.get("external_url")
        ),
        copyright=str(item.get("copyright") or ""),
        publisher=str(item.get("label") or ""),
        composer=_text(item.get("composer")),
        genre=_text(item.get("genre")),
        upc=str(item.get("upc") or ""),
        album_type=str(item.get("album_type") or ""),
        preview_url=str(item.get("preview_url") or ""),
        album_id=str(item.get("album_id") or ""),
        album_url=str(item.get("album_url") or ""),
        artist_id=str(item.get("artist_id") or ""),
        artist_url=str(item.get("artist_url") or ""),
        is_explicit=bool(item.get("explicit")),
    )


class ExtensionUrlClient:
    """`get_url_async` over an extension's `handleUrl`, in the shape the
    built-in clients answer: `(name, tracks, cover, meta)`."""

    def __init__(
        self, ext_name: str, service: str, *, provider_factory: Any = None
    ) -> None:
        self.ext_name = ext_name
        self.service = service
        self._provider_factory = provider_factory

    def _call_sync(self, method: str, *args: Any) -> Any:
        if self._provider_factory is not None:
            provider = self._provider_factory(self.ext_name)
        else:
            from ..extensions.provider import JSExtensionProvider

            provider = JSExtensionProvider(self.ext_name, timeout_s=EXTENSION_TIMEOUT_S)
        try:
            return provider._call(method, *args)
        finally:
            close = getattr(provider, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    pass

    async def get_url_async(self, url: str, include_featuring: bool = True, **_: Any):
        response = await asyncio.to_thread(self._call_sync, "handleUrl", url)
        if not isinstance(response, dict) or response.get("success") is False:
            reason = response.get("error") if isinstance(response, dict) else ""
            raise SpotiflacError(
                ErrorKind.TRACK_NOT_FOUND,
                f"The '{self.ext_name}' extension could not read this link"
                + (f": {reason}" if reason else "."),
                provider=f"ext:{self.ext_name}",
            )

        kind = str(response.get("type") or "")
        if kind == "track" and isinstance(response.get("track"), dict):
            track = track_from_extension(response["track"], self.service)
            tracks = [track] if track else []
            name = track.title if track else ""
            cover = track.cover_url if track else ""
        else:
            node = response.get(kind) if isinstance(response.get(kind), dict) else {}
            collection = {
                **node,
                "type": kind,
                "name": response.get("name") or node.get("name"),
                "cover_url": response.get("cover_url") or node.get("cover_url"),
            }
            items = response.get("tracks") or node.get("tracks") or []
            tracks = [
                t
                for item in items
                if (t := track_from_extension(item, self.service, collection))
            ]
            name = _text(collection["name"])
            cover = _first_image(collection)

        if not tracks:
            raise SpotiflacError(
                ErrorKind.TRACK_NOT_FOUND,
                f"The '{self.ext_name}' extension found no tracks behind this {kind or 'link'}.",
                provider=f"ext:{self.ext_name}",
            )
        return name, tracks, cover, {}


async def get_url_with_fallback(
    url: str,
    client_factory: Callable[[], Any],
    *,
    manager: Any = None,
    provider_factory: Any = None,
    **kwargs: Any,
):
    """The built-in client's answer for `url`, or an extension's when that
    client fails or finds nothing.

    `client_factory` rather than a client: building the Spotify client is
    itself a network round trip (a session, a token), and that is exactly
    where a change on Spotify's side shows up first.

    A malformed link is not retried elsewhere — no other reader will make
    sense of it. When there is no extension to ask, or it fails too, the
    built-in client's own error is what the caller sees.
    """
    from ..downloader import _call_metadata_get_url

    failure: BaseException | None = None
    result: Any = None
    try:
        client = client_factory()
        result = await _call_metadata_get_url(client, url, **kwargs)
        if result and len(result) > 1 and result[1]:
            return result
    except SpotiflacError as exc:
        if exc.kind == ErrorKind.INVALID_URL:
            raise
        failure = exc
    except Exception as exc:
        failure = exc

    service = native_service_for(url)
    ext_name = fallback_extension_for(url, manager) if service else None
    if not ext_name:
        if failure is not None:
            raise failure
        return result

    logger.warning(
        "[metadata] The built-in %s client %s; resolving %s with the '%s' extension instead.",
        _LABELS[service],
        f"failed ({failure})" if failure is not None else "found no tracks",
        url,
        ext_name,
    )
    try:
        return await ExtensionUrlClient(
            ext_name, service, provider_factory=provider_factory
        ).get_url_async(url)
    except Exception as ext_exc:
        logger.warning(
            "[metadata] The '%s' extension could not resolve %s either: %s",
            ext_name,
            url,
            ext_exc,
        )
        if failure is not None:
            raise failure from None
        return result
