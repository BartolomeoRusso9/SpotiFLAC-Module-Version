"""extension_enrichment.py — The extensions' own metadata, for tagging.

Every JavaScript download extension (tidal-web, qobuz-web, deezer) and the
apple-music metadata extension answers `enrichTrack`: given a title, artist,
ISRC and running time, it finds the track on its service and returns what
that service knows — label, ℗ line, composers, UPC, the release's totals,
its credits. The app never asked. Enrichment went only to the built-in
Python lookups, which for Tidal returned nothing at all and for Apple only
what the public iTunes search carries.

So enrichment now asks both. Each service in the enrichment list gets its
built-in lookup (which uses the Python extensions where it needs a
service's API) and then its own JavaScript extensions; after those, every
other installed JavaScript extension that implements enrichTrack is asked
too — always, whether or not its service is in the list. Each fills only
the fields still blank.

Two things keep that from costing more than it gives:

* the extensions run in Node, and starting a runtime takes seconds; the
  providers here are kept alive and reused across tracks, so only the first
  track of a run pays for it;
* an extension's answer is checked before it is believed — the title (and
  ISRC, where both have one) must be the same recording, and the
  release-scoped fields (label, UPC, date, totals, cover) are taken only
  when the album agrees, the same rule Deezer's lookup already follows.

$SPOTIFLAC_ENRICH_EXTENSIONS=0 turns the extension half off.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from .extension_metadata import _int, _text
from .isrc_utils import normalize_isrc
from .metadata_enrichment import (
    EnrichedMetadata,
    _apply_credits,
    _other_recording,
    _same_release,
)

logger = logging.getLogger(__name__)

ENABLE_ENV = "SPOTIFLAC_ENRICH_EXTENSIONS"

#: How long one extension may take for one track. The first call of a run
#: also starts its Node runtime.
EXTENSION_TIMEOUT_S = 20.0

#: How long the installed-extension listing is reused before the disk is
#: read again — enrichment runs once per track.
_LIST_TTL_S = 60.0


def extension_enrichment_enabled() -> bool:
    return os.environ.get(ENABLE_ENV, "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


# ---------------------------------------------------------------------------
# Which extensions
# ---------------------------------------------------------------------------

_installed_cache: tuple[float, list] | None = None
_installed_lock = threading.Lock()


def _installed_extensions(manager: Any = None) -> list:
    global _installed_cache
    if manager is not None:
        return list(manager.list_installed())
    with _installed_lock:
        now = time.monotonic()
        if _installed_cache and now - _installed_cache[0] < _LIST_TTL_S:
            return _installed_cache[1]
        from ..extensions.manager import ExtensionManager

        installed = ExtensionManager(auto_install_downloads=False).list_installed()
        _installed_cache = (now, installed)
        return installed


_ENRICH_TRACK_RE = re.compile(r"\benrichTrack\b")

#: entry point → (mtime, whether it defines enrichTrack). Read once per file
#: version: enrichment runs per track and the answer only changes when the
#: extension is updated.
_implements_cache: dict[str, tuple[float, bool]] = {}


def implements_enrich_track(ext: Any) -> bool:
    """Whether an installed extension's code defines `enrichTrack`.

    Read from the source rather than by calling it: finding out by calling
    would start a Node runtime for every extension that has none, and an
    extension without the function answers the call with an error anyway.
    """
    try:
        path = Path(ext.entry_point)
        mtime = path.stat().st_mtime
    except (OSError, AttributeError):
        return False
    key = str(path)
    cached = _implements_cache.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        found = bool(
            _ENRICH_TRACK_RE.search(path.read_text(encoding="utf-8", errors="replace"))
        )
    except OSError:
        return False
    _implements_cache[key] = (mtime, found)
    return found


def can_enrich(ext: Any) -> bool:
    """A JavaScript metadata extension that implements enrichTrack — the
    only kind enrichment asks, and every one of them is asked."""
    return (
        ext.runtime == "javascript"
        and "metadata_provider" in (ext.types or [])
        and implements_enrich_track(ext)
    )


def enrichment_extensions(manager: Any = None) -> list[str]:
    """Every installed extension that can enrich, whatever service it is
    for — not only those of the services in the enrichment list."""
    return sorted(ext.name for ext in _installed_extensions(manager) if can_enrich(ext))


def extensions_for_service(service: str, manager: Any = None) -> list[str]:
    """The enriching extensions of one enrichment service ("tidal" →
    tidal-web, "apple" → apple-music, …), so they can be merged right after
    that service's built-in lookup."""
    from ..extensions.catalog import canonical_service_name

    return [
        ext.name
        for ext in _installed_extensions(manager)
        if can_enrich(ext) and canonical_service_name(ext.name) == service
    ]


# ---------------------------------------------------------------------------
# Long-lived providers
# ---------------------------------------------------------------------------

_providers: dict[str, Any] = {}
_providers_lock = threading.Lock()


def _provider(ext_name: str, factory: Any = None) -> Any:
    with _providers_lock:
        provider = _providers.get(ext_name)
        if provider is None:
            if factory is not None:
                provider = factory(ext_name)
            else:
                from ..extensions.provider import JSExtensionProvider

                provider = JSExtensionProvider(
                    ext_name, timeout_s=int(EXTENSION_TIMEOUT_S)
                )
            _providers[ext_name] = provider
        return provider


def close_extension_providers() -> None:
    """Stops every kept runtime. Also what picks up an updated extension:
    the next call starts one on the new code."""
    with _providers_lock:
        providers = list(_providers.values())
        _providers.clear()
    for provider in providers:
        close = getattr(provider, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                pass


atexit.register(close_extension_providers)


# ---------------------------------------------------------------------------
# An extension's track, as enrichment
# ---------------------------------------------------------------------------


def _joined(value: Any) -> str:
    if isinstance(value, list):
        return "; ".join(dict.fromkeys(n for n in (_text(v) for v in value) if n))
    return _text(value)


def enriched_from_track(
    track: Any,
    *,
    title: str = "",
    isrc: str = "",
    album: str = "",
    source: str = "",
) -> EnrichedMetadata:
    """What an extension's track dict tells us, once it is known to be ours.

    `title` and `isrc` identify the recording asked for; an answer about a
    different one is discarded whole. `album` names the release; when the
    extension's differs, only what belongs to the recording (genre,
    composers, credits, explicit) is kept. Pass no `album` for the track a
    download actually fetched — that release is the file's.
    """
    out = EnrichedMetadata()
    if not isinstance(track, dict):
        return out

    found_title = _text(track.get("name") or track.get("title"))
    if _other_recording(title, found_title):
        logger.debug(
            "[meta/ext] %s answered for %r, not %r — ignored",
            source,
            found_title,
            title,
        )
        return out
    found_isrc = normalize_isrc(str(track.get("isrc") or ""))
    wanted_isrc = normalize_isrc(isrc or "")
    if wanted_isrc and found_isrc and wanted_isrc != found_isrc:
        logger.debug(
            "[meta/ext] %s answered with ISRC %s, not %s — ignored",
            source,
            found_isrc,
            wanted_isrc,
        )
        return out

    out.isrc = found_isrc
    out.genre = _joined(track.get("genre"))
    out.composer = _joined(track.get("composer"))
    out.bpm = _int(track.get("bpm"))
    out.explicit = track.get("explicit") is True

    found_album = _text(track.get("album_name") or track.get("album"))
    release_ok = not album or not found_album or _same_release(album, found_album)
    if release_ok:
        out.label = _joined(track.get("label"))
        out.copyright = str(track.get("copyright") or "").strip()
        out.upc = str(track.get("upc") or "").strip()
        out.release_date = str(track.get("release_date") or "")[:10]
        out.total_tracks = _int(track.get("total_tracks"))
        out.total_discs = _int(track.get("total_discs"))
        out.album_type = str(track.get("album_type") or "")
        cover = str(track.get("cover_url") or "")
        if cover.startswith("http"):
            out.cover_url_hd = cover
    else:
        logger.debug(
            "[meta/ext] %s: release %r is not %r — keeping recording fields only",
            source,
            found_album,
            album,
        )

    # tidal-web keeps the service's credits as
    # [{"type": "Producer", "contributors": [{"name": ...}]}, ...].
    credits = track.get("credits")
    if isinstance(credits, list):
        pairs = [
            (contributor.get("name"), credit.get("type"))
            for credit in credits
            if isinstance(credit, dict)
            for contributor in credit.get("contributors") or []
            if isinstance(contributor, dict)
        ]
        _apply_credits(out, pairs)
    for field in ("lyricist", "producer"):
        if track.get(field) and not getattr(out, field):
            setattr(out, field, _joined(track.get(field)))
    return out


async def fetch_extension_enrichment(
    ext_name: str,
    track_name: str,
    artist_name: str,
    isrc: str = "",
    album_name: str = "",
    duration_ms: int = 0,
    *,
    provider_factory: Any = None,
) -> EnrichedMetadata:
    payload = {
        "name": track_name,
        "artists": artist_name,
        "isrc": isrc,
        "album_name": album_name,
        "duration_ms": duration_ms,
    }

    def call() -> Any:
        return _provider(ext_name, provider_factory)._call("enrichTrack", payload, {})

    try:
        result = await asyncio.to_thread(call)
    except Exception as exc:
        logger.debug("[meta/ext] %s enrichTrack failed: %s", ext_name, exc)
        return EnrichedMetadata()
    return enriched_from_track(
        result, title=track_name, isrc=isrc, album=album_name, source=ext_name
    )
