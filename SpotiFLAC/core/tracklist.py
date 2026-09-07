"""tracklist.py — Turning a link into the tracks behind it.

Between "here is an album URL" and "download these" there is a step every
frontend needs and each one had been writing for itself: pick the metadata
client that knows the domain, ask it, and unpack an answer whose shape varies
by provider — `(name, tracks)`, or with a cover, or with a metadata dict.

The GUI did it inline in `_fetch_metadata_task`; the terminal UI needs the
same thing to show a track list you can pick from. Two copies of a
domain-to-client mapping is one too many: a provider added to one would be
missing from the other, and the symptom is a link that works in one window
and not in the other.

Nothing here downloads, and nothing prints.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .url_utils import url_host_matches


@dataclass
class Tracklist:
    """What a link turned out to contain."""

    name: str = ""
    tracks: list[Any] = field(default_factory=list)
    cover: str = ""
    meta: dict = field(default_factory=dict)
    #: The link it came from, kept so a caller can tell "all of it" from
    #: "these three of it" later without holding the URL separately.
    source_url: str = ""

    def __len__(self) -> int:
        return len(self.tracks)

    def __bool__(self) -> bool:
        return bool(self.tracks)


def metadata_client_for(url: str):
    """The client that knows this URL's domain.

    Anything unrecognised goes to Spotify, which is both the common case and
    the one that understands `spotify:` URIs and bare search text.
    """
    if url_host_matches(url, "tidal.com"):
        from .tidal_metadata import TidalMetadataClient

        return TidalMetadataClient()
    if url_host_matches(url, "music.apple.com"):
        from .apple_music_metadata import AppleMusicMetadataClient

        return AppleMusicMetadataClient()
    from .spotify_metadata import SpotifyMetadataClient

    return SpotifyMetadataClient()


def is_link(value: str) -> bool:
    """Whether this is something to fetch rather than something to search."""
    return (value or "").strip().startswith(("http://", "https://", "spotify:"))


async def resolve_tracklist(url: str, *, include_featuring: bool = True) -> Tracklist:
    """Fetches the tracks behind *url*.

    Raises whatever the provider raises: a caller showing this on screen wants
    the reason, and swallowing it here would leave every failure looking like
    an empty album.
    """
    stripped = (url or "").strip()
    if not stripped:
        return Tracklist(source_url="")

    # Imported here rather than at module scope: this pulls in the downloader,
    # and resolving a link should not cost that until someone resolves one.
    from ..downloader import _call_metadata_get_url

    client = metadata_client_for(stripped)
    result = await _call_metadata_get_url(
        client,
        stripped,
        include_featuring=include_featuring,
    )

    return Tracklist(
        name=result[0] if result else "",
        tracks=list(result[1]) if len(result) > 1 and result[1] else [],
        cover=result[2] if len(result) > 2 else "",
        meta=result[3] if len(result) > 3 else {},
        source_url=stripped,
    )


def track_url(track: Any, source_url: str = "") -> str:
    """The link that stands for one track, so it can be downloaded alone.

    Providers are inconsistent about this: some hand back a full URL, some
    only an id. The id is reconstructed against whichever service the
    collection came from — the same fallback the GUI's batch download uses,
    because a track id is only meaningful next to the service that issued it.
    """
    url = getattr(track, "external_url", None) or getattr(track, "url", None)
    if url:
        return str(url)

    track_id = getattr(track, "id", None)
    if not track_id:
        return ""

    source = (source_url or "").strip()
    # Matched on the host, not on the whole string: an album slug is free to
    # contain the word "tidal" or "apple", and a substring check on the URL
    # let a Spotify playlist called "apple of my eye" mint Apple Music links
    # for every track in it.
    if url_host_matches(source, "tidal.com"):
        return f"https://tidal.com/browse/track/{track_id}"
    if url_host_matches(source, "music.apple.com"):
        # Not `/track/{id}`: Apple Music has no such path, and
        # parse_apple_music_url() rejects it outright — the storefront and
        # the `song` segment are both required for a link to resolve. The
        # slug between them is decorative, so the title stands in for it.
        return (
            f"https://music.apple.com/{_apple_storefront(source)}"
            f"/song/{_slug(getattr(track, 'title', '')) or 'song'}/{track_id}"
        )
    # A bare `spotify:track:...` URI has no host to match, so it is named
    # here rather than left to the fallback.
    if url_host_matches(source, "open.spotify.com", "spotify.com") or source.startswith(
        "spotify:"
    ):
        return f"https://open.spotify.com/track/{track_id}"
    if not source:
        return f"https://open.spotify.com/track/{track_id}"
    return ""


def _apple_storefront(source_url: str) -> str:
    """The two-letter storefront out of an Apple Music URL, or Apple's own
    default when the source does not name one.
    """
    match = re.search(r"music\.apple\.com/([a-z]{2})/", source_url, re.IGNORECASE)
    return match.group(1).lower() if match else "us"


def _slug(text: str) -> str:
    """A URL path segment from a track title — decorative, not identifying."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return cleaned[:60]


def download_target(
    tracklist: Tracklist,
    selected: list[int] | None = None,
) -> str | list[str]:
    """What to hand the downloader for a whole list, or part of one.

    Selecting everything gives back the collection URL rather than a list of
    track links, which is not merely tidier: the collection path resolves once
    and keeps the album's own ordering and numbering, where a list of
    individual tracks is a list of individual tracks.

    `_run_download_async` takes either, which is what makes both possible.
    """
    if selected is None or len(selected) == len(tracklist.tracks):
        if tracklist.source_url:
            return tracklist.source_url

    # `is None`, not falsiness: an empty selection means "none of them", and
    # `selected or range(...)` read that as "no opinion" and downloaded the
    # lot — the one mistake here that costs bandwidth rather than a message.
    indices = range(len(tracklist.tracks)) if selected is None else selected

    urls: list[str] = []
    for index in sorted(indices):
        if 0 <= index < len(tracklist.tracks):
            url = track_url(tracklist.tracks[index], tracklist.source_url)
            if url:
                urls.append(url)
    return urls


def unresolved_titles(
    tracklist: Tracklist,
    selected: list[int] | None = None,
) -> list[str]:
    """Selected tracks with no link, which cannot be fetched on their own.

    A CSV of bare titles hits this for every row. Naming them once is more
    use than a message per track, and more honest than silently downloading
    fewer than were asked for.
    """
    indices = range(len(tracklist.tracks)) if selected is None else selected

    missing: list[str] = []
    for index in sorted(indices):
        if not (0 <= index < len(tracklist.tracks)):
            continue
        track = tracklist.tracks[index]
        if not track_url(track, tracklist.source_url):
            missing.append(str(getattr(track, "title", "") or "unknown"))
    return missing
