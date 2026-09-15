"""extension_metadata.py — Catalogues that only an extension can read.

Melon, Bugs and Genie have no public API. What they have is extensions,
written for the mobile app, that scrape them and answer as a
`metadata_provider`: `searchTracks`, `getTrack`, `getAlbum`, `getPlaylist`.
Those extensions installed fine here and then were never asked anything —
nothing in this app routed a link or a search to a metadata provider.

This module is that routing:

* a link on one of the sites below is handed to whichever installed
  extension declares network access to that site, and its answer becomes
  `TrackMetadata` like any other provider's;
* a search can be sent to the same extension instead of Spotify;
* each track is matched to Spotify by title and artist to learn its ISRC,
  because these catalogues rarely publish one and the download services find
  a recording by ISRC. Tags keep the catalogue's own names — a Korean title
  stays in Hangul — only the identifier is borrowed.

The extensions carry no URL handling of their own, so the link shapes are
listed here, one entry per site. The extension is found by the hosts in its
manifest's `permissions.network`, not by its id: a fork or a renamed
extension for the same site works without a change here.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

from .errors import ErrorKind, SpotiflacError
from .models import TrackMetadata
from .text_match import (
    DURATION_TOLERANCE_MS,
    score_track_match,
    titles_match,
    variant_conflict,
)
from .url_utils import url_host_matches

logger = logging.getLogger(__name__)

#: A Spotify candidate at or above this score is taken without further
#: checks. Below it, an exact title still wins when the running time agrees
#: — see `_pick_spotify_match` for why the artist cannot always vote.
STRONG_MATCH = 0.8

#: Spotify lookups for one collection, in flight at once.
MATCH_CONCURRENCY = 4

#: How long one extension call may take. These extensions scrape HTML and
#: some fall back through several pages before answering.
EXTENSION_TIMEOUT_S = 90


@dataclass(frozen=True)
class CatalogueSite:
    """One site an extension can read, and the link shapes it uses."""

    key: str
    label: str
    hosts: tuple[str, ...]
    #: `(kind, regex)` tried in order against the whole link; the first
    #: group is the id. Order matters where a link carries more than one id.
    patterns: tuple[tuple[str, str], ...]
    track_url: str
    album_url: str
    playlist_url: str = ""

    def url_for(self, kind: str, item_id: str) -> str:
        template = {
            "track": self.track_url,
            "album": self.album_url,
            "playlist": self.playlist_url,
        }.get(kind, "")
        return template.format(id=item_id) if template and item_id else ""


SITES: tuple[CatalogueSite, ...] = (
    CatalogueSite(
        key="melon",
        label="Melon",
        hosts=("melon.com",),
        patterns=(
            ("playlist", r"[?&]plylstSeq=(\d+)"),
            ("album", r"[?&]albumId=(\d+)"),
            ("track", r"[?&]songId=(\d+)"),
        ),
        track_url="https://www.melon.com/song/detail.htm?songId={id}",
        album_url="https://www.melon.com/album/detail.htm?albumId={id}",
        playlist_url=(
            "https://www.melon.com/mymusic/dj/mymusicdjplaylistview_inform.htm"
            "?plylstSeq={id}"
        ),
    ),
    CatalogueSite(
        key="bugs",
        label="Bugs",
        hosts=("bugs.co.kr",),
        patterns=(
            ("album", r"/album/(\d+)"),
            ("track", r"/track/(\d+)"),
        ),
        track_url="https://music.bugs.co.kr/track/{id}",
        album_url="https://music.bugs.co.kr/album/{id}",
    ),
    CatalogueSite(
        key="genie",
        label="Genie",
        hosts=("genie.co.kr",),
        patterns=(
            ("album", r"[?&]axnm=(\d+)"),
            ("track", r"[?&]xgnm=(\d+)"),
        ),
        track_url="https://www.genie.co.kr/detail/songInfo?xgnm={id}",
        album_url="https://www.genie.co.kr/detail/albumInfo?axnm={id}",
    ),
)


@dataclass(frozen=True)
class CatalogueLink:
    site: CatalogueSite
    kind: str
    item_id: str


def site_for_url(url: str) -> CatalogueSite | None:
    for site in SITES:
        if url_host_matches(url, *site.hosts):
            return site
    return None


def parse_catalogue_url(url: str) -> CatalogueLink | None:
    """The site, kind and id behind a link, or None for anything else —
    including a link on a known site that points at no track, album or
    playlist (a chart page, an artist)."""
    site = site_for_url(url or "")
    if site is None:
        return None
    for kind, pattern in site.patterns:
        match = re.search(pattern, url)
        if match:
            return CatalogueLink(site=site, kind=kind, item_id=match.group(1))
    return None


# ---------------------------------------------------------------------------
# Finding the extension
# ---------------------------------------------------------------------------


def _host_covers(declared: str, site_host: str) -> bool:
    declared = str(declared or "").lower().strip().lstrip("*.").strip(".")
    return declared == site_host or declared.endswith("." + site_host)


def site_for_extension(ext: Any) -> CatalogueSite | None:
    """The site an installed extension reads, judged by its manifest."""
    if "metadata_provider" not in (getattr(ext, "types", None) or []):
        return None
    manifest = getattr(ext, "manifest", None) or {}
    permissions = manifest.get("permissions") or {}
    declared = permissions.get("network") if isinstance(permissions, dict) else None
    if not isinstance(declared, list):
        return None
    for site in SITES:
        if any(
            _host_covers(host, site_host)
            for host in declared
            for site_host in site.hosts
        ):
            return site
    return None


def catalogue_extensions(manager: Any = None) -> list[tuple[Any, CatalogueSite]]:
    """Every installed extension that reads one of `SITES`, with its site."""
    if manager is None:
        from ..extensions.manager import ExtensionManager

        manager = ExtensionManager(auto_install_downloads=False)
    try:
        installed = manager.list_installed()
    except Exception as exc:
        logger.debug("[catalogue] could not list extensions: %s", exc)
        return []
    found = []
    for ext in installed:
        site = site_for_extension(ext)
        if site is not None:
            found.append((ext, site))
    return found


def metadata_sources(manager: Any = None) -> list[dict[str, str]]:
    """What a search can be sent to: Spotify, then each catalogue extension."""
    sources = [{"id": "spotify", "label": "Spotify"}]
    for ext, site in catalogue_extensions(manager):
        sources.append(
            {
                "id": ext.name,
                "label": getattr(ext, "display_name", "") or site.label,
                "site": site.key,
            }
        )
    return sources


# ---------------------------------------------------------------------------
# Turning an extension's answer into TrackMetadata
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    if isinstance(value, list):
        names = [_text(v) for v in value]
        return ", ".join(n for n in names if n)
    if isinstance(value, dict):
        return _text(value.get("name") or value.get("title"))
    return str(value or "").strip()


def _first_image(item: dict) -> str:
    cover = item.get("cover_url") or item.get("cover") or ""
    if cover:
        return str(cover)
    images = item.get("images")
    if isinstance(images, list):
        for image in images:
            url = image.get("url") if isinstance(image, dict) else image
            if url:
                return str(url)
    return ""


def _int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


# The extensions scrape whole table rows, and the row's buttons and badges
# come along with the title: Genie's "TITLE" badge in front of a title track,
# Melon's hidden "곡 선택" (select song) checkbox label and "좋아요" (like)
# button after it. Left in, they end up in the file's tags and name, and they
# stop the title from matching Spotify's.
# Case-sensitive on purpose: the badge is upper case, and "Title Track" is a
# name a song can have.
_TITLE_BADGE_RE = re.compile(r"^(?:TITLE|타이틀곡?)\s+")
_TITLE_TRAIL_RE = re.compile(r"\s+(?:곡\s*선택|좋아요|곡정보|재생|담기)$")

#: A title that is nothing but the badge: Melon's album page puts "Title" in
#: the name cell of the title track and the name itself somewhere the
#: extension does not look.
_BARE_BADGES = {"title", "타이틀", "타이틀곡"}

#: The notice Bugs shows in place of a track page to a browser with no
#: player chosen — "안내 음악을 재생할 플레이어를 선택해 주세요…".
_NOTICE_RE = re.compile(r"^안내\s.*플레이어")

#: Korean catalogues mark instrumentals "(Inst.)" or "(MR)", which
#: text_match's list of different-recording markers does not know.
_KOREAN_VARIANT_RE = re.compile(
    r"[(\[]\s*(?:inst\.?|mr|off[- ]?vocal)\s*[)\]]", re.IGNORECASE
)

#: What an extension reports as an album name when it grabbed a page's
#: badge, notice or site name instead.
_NOT_ALBUM_NAMES = {
    "[타이틀곡]",
    "타이틀곡",
    "안내",
    "melon",
    "melon music",
    "bugs",
    "bugs music",
    "genie",
    "genie music",
}

#: A title longer than this is a page's text, not a song's name — Bugs'
#: track page yields its "choose a player" notice.
_MAX_TITLE_LENGTH = 150


def clean_title(value: str) -> str:
    """A song title without the page furniture around it, or "" when there
    is no title in it at all."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) > _MAX_TITLE_LENGTH or _NOTICE_RE.search(text):
        return ""
    for _ in range(3):
        stripped = _TITLE_TRAIL_RE.sub("", _TITLE_BADGE_RE.sub("", text)).strip()
        if stripped == text:
            break
        text = stripped
    return "" if text.lower() in _BARE_BADGES else text


def _different_recording(ours: str, theirs: str) -> bool:
    korean_mark = bool(_KOREAN_VARIANT_RE.search(ours or "")) != bool(
        _KOREAN_VARIANT_RE.search(theirs or "")
    )
    return korean_mark or variant_conflict(ours, theirs)


def clean_album(value: str, artists: str = "") -> str:
    """An album name without the page-title dressing some extensions return
    ("신사와 아가씨 OST Part.2 / 임영웅 - genie", "SCENEDROME - RESCENE (리센느)")."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(r"\s+-\s+genie$", "", text, flags=re.IGNORECASE)
    if text.lower() in _NOT_ALBUM_NAMES or text.startswith("안내 "):
        return ""
    artist_names = [a.strip().lower() for a in (artists or "").split(",") if a.strip()]
    tail = re.search(r"\s+[-/]\s+([^-/]+)$", text)
    if tail and artist_names:
        credited = tail.group(1).strip().lower()
        if any(name in credited or credited in name for name in artist_names):
            text = text[: tail.start()].strip()
    return text


def _known(value: str) -> bool:
    return bool(value) and value != "Unknown"


def to_track(
    item: dict,
    site: CatalogueSite,
    *,
    position: int = 0,
    total: int = 0,
    collection: dict | None = None,
) -> TrackMetadata | None:
    """One track as an extension describes it, as the downloader needs it.

    `collection` is the album or playlist it came in, used for whatever the
    track itself leaves out. Album numbering is only trusted from an album:
    a playlist's position is not a track number.
    """
    if not isinstance(item, dict):
        return None
    raw_id = str(item.get("id") or "").split(":")[-1]
    title = clean_title(_text(item.get("name") or item.get("title")))
    if not raw_id or not title:
        return None

    collection = collection or {}
    from_album = collection.get("type") == "album"
    album_node = item.get("album") if isinstance(item.get("album"), dict) else {}

    artists = _text(item.get("artists") or item.get("artist"))
    if not artists and from_album:
        artists = _text(collection.get("artists"))

    raw_album = _text(item.get("album_name") or album_node.get("name"))
    # Melon's track page gives no artist, and the "album" it reports is the
    # page's og:title — "<song> - <artist>". That is the artist, then, and
    # not an album name.
    page_title = re.match(rf"^{re.escape(title)}\s+-\s+(.+)$", raw_album)
    if not artists and page_title:
        artists, raw_album = page_title.group(1).strip(), ""

    album_id = str(item.get("album_id") or album_node.get("id") or "")
    if not album_id and from_album:
        album_id = str(collection.get("id") or "")
    album_name = clean_album(raw_album, artists)
    if not album_name and from_album:
        album_name = clean_album(_text(collection.get("name")), artists)

    album_artist = _text(item.get("album_artist"))
    if (
        not album_artist
        and from_album
        and collection.get("album_type") != "compilation"
    ):
        album_artist = _text(collection.get("artists"))

    track_number = _int(item.get("track_number"))
    if not track_number and from_album:
        track_number = position

    return TrackMetadata(
        id=f"{site.key}:{raw_id}",
        title=title,
        artists=artists,
        album=album_name,
        album_artist=album_artist or artists,
        isrc=str(item.get("isrc") or "").strip().upper(),
        track_number=track_number,
        disc_number=_int(item.get("disc_number")) or 1,
        total_tracks=_int(item.get("total_tracks")) or (total if from_album else 0),
        duration_ms=_int(item.get("duration_ms")),
        release_date=str(
            item.get("release_date") or collection.get("release_date") or ""
        ),
        cover_url=_first_image(item)
        or album_node.get("cover_url", "")
        or _first_image(collection),
        external_url=site.url_for("track", raw_id),
        album_id=album_id,
        album_url=site.url_for("album", album_id),
        album_type=str(collection.get("album_type") or ""),
    )


# ---------------------------------------------------------------------------
# Borrowing the ISRC from Spotify
# ---------------------------------------------------------------------------


def _pick_spotify_match(track: TrackMetadata, candidates: list) -> Any | None:
    """The Spotify result that is this recording, or None.

    A strong score is enough on its own. Below it, the artist is the weak
    vote: Melon writes 아이유 where Spotify writes IU, which scores as a
    different artist while being the same one. So an exact title is accepted
    without the artist agreeing — but only among results for a query that
    already named the artist, and only when the running time does not
    contradict it. With no artist at all, the running time has to be known.

    A different recording under the same name — the "(Inst.)" next to the
    original on an OST single — is never taken: the titles match once the
    decoration is stripped, and Genie gives both the same running time.
    """
    candidates = [
        c
        for c in candidates
        if not _different_recording(track.title, getattr(c, "title", ""))
    ]
    best, best_score = None, 0.0
    for candidate in candidates:
        score = score_track_match(
            title=track.title,
            artist=track.artists,
            album=track.album,
            duration_ms=track.duration_ms,
            candidate=candidate,
        )
        if score > best_score:
            best, best_score = candidate, score
    if best is not None and best_score >= STRONG_MATCH:
        return best

    if not _known(track.artists) and not track.duration_ms:
        return None
    for candidate in candidates:
        if not titles_match(track.title, getattr(candidate, "title", "")):
            continue
        theirs = _int(getattr(candidate, "duration_ms", 0))
        if (
            track.duration_ms
            and theirs
            and abs(track.duration_ms - theirs) > DURATION_TOLERANCE_MS
        ):
            continue
        return candidate
    return None


async def attach_isrcs(
    tracks: list[TrackMetadata],
    *,
    spotify: Any = None,
    isrc_helper: Any = None,
) -> list[TrackMetadata]:
    """Fills in the ISRC of every track that lacks one, where Spotify has it.

    A track Spotify cannot place is left as it is: the download services
    still get its title and artist to search by.
    """
    missing = [i for i, t in enumerate(tracks) if not t.isrc]
    if not missing:
        return tracks

    if spotify is None:
        from .spotify_metadata import SpotifyMetadataClient

        # Its constructor opens a session over the network.
        spotify = await asyncio.to_thread(SpotifyMetadataClient)
    if isrc_helper is None:
        from .http import AsyncHttpClient
        from .isrc_helper import IsrcHelper

        isrc_helper = IsrcHelper(AsyncHttpClient("isrc"))

    semaphore = asyncio.Semaphore(MATCH_CONCURRENCY)
    out = list(tracks)

    async def _one(index: int) -> None:
        track = out[index]
        first_artist = track.artists.split(",")[0] if _known(track.artists) else ""
        query = " ".join(p for p in (track.title, first_artist) if p)
        async with semaphore:
            try:
                candidates = await spotify.search_tracks_async(query, limit=10)
                match = _pick_spotify_match(track, list(candidates or []))
                if match is None:
                    logger.debug("[catalogue] no Spotify match for %s", query)
                    return
                isrc = getattr(match, "isrc", "") or await isrc_helper.get_isrc_async(
                    match.id
                )
            except Exception as exc:
                logger.debug("[catalogue] Spotify match failed for %s: %s", query, exc)
                return
        if isrc:
            update = {
                "isrc": isrc,
                "duration_ms": track.duration_ms
                or _int(getattr(match, "duration_ms", 0)),
                "release_date": track.release_date
                or str(getattr(match, "release_date", "") or ""),
            }
            # Only what the catalogue left blank: its own names stay.
            for field in ("artists", "album"):
                theirs = str(getattr(match, field, "") or "")
                if not _known(getattr(track, field)) and theirs:
                    update[field] = theirs
            if not _known(track.album_artist) and "artists" in update:
                update["album_artist"] = update["artists"]
            out[index] = track.model_copy(update=update)

    await asyncio.gather(*(_one(i) for i in missing))
    return out


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


class ExtensionMetadataClient:
    """A metadata client, like the Spotify or Tidal one, backed by an
    installed catalogue extension."""

    def __init__(
        self,
        ext_name: str,
        site: CatalogueSite,
        *,
        provider_factory: Any = None,
        match_isrcs: bool = True,
    ) -> None:
        self.ext_name = ext_name
        self.site = site
        self.match_isrcs = match_isrcs
        self._provider_factory = provider_factory

    @classmethod
    def for_url(cls, url: str, manager: Any = None) -> ExtensionMetadataClient:
        link = parse_catalogue_url(url)
        if link is None:
            raise SpotiflacError(ErrorKind.INVALID_URL, f"Unsupported link: {url}")
        for ext, site in catalogue_extensions(manager):
            if site.key == link.site.key:
                return cls(ext.name, site)
        raise SpotiflacError(
            ErrorKind.UNAVAILABLE,
            f"{link.site.label} links need a {link.site.label} metadata extension. "
            "Install one from the extension registry first.",
        )

    @classmethod
    def for_source(cls, name: str, manager: Any = None) -> ExtensionMetadataClient:
        for ext, site in catalogue_extensions(manager):
            if ext.name == name:
                return cls(ext.name, site)
        raise SpotiflacError(
            ErrorKind.UNAVAILABLE,
            f"No catalogue extension named '{name}' is installed.",
        )

    # -- calling the extension ------------------------------------------------

    def _make_provider(self):
        if self._provider_factory is not None:
            return self._provider_factory(self.ext_name)
        from ..extensions.provider import JSExtensionProvider

        return JSExtensionProvider(self.ext_name, timeout_s=EXTENSION_TIMEOUT_S)

    def _call_sync(self, method: str, *args: Any) -> Any:
        # One provider per operation, closed afterwards: each holds Node
        # processes, and a client lives as long as whoever asked for it.
        provider = self._make_provider()
        try:
            return provider._call(method, *args)
        finally:
            close = getattr(provider, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    pass

    async def _call(self, method: str, *args: Any) -> Any:
        return await asyncio.to_thread(self._call_sync, method, *args)

    # -- links ------------------------------------------------------------------

    def info_for(self, url: str) -> dict:
        link = parse_catalogue_url(url)
        return {"type": link.kind, "id": link.item_id} if link else {}

    async def get_url_async(self, url: str, include_featuring: bool = True, **_: Any):
        """`(name, tracks, cover)` for a track, album or playlist link."""
        link = parse_catalogue_url(url)
        if link is None or link.site.key != self.site.key:
            raise SpotiflacError(ErrorKind.INVALID_URL, f"Unsupported link: {url}")

        method = {"track": "getTrack", "album": "getAlbum", "playlist": "getPlaylist"}[
            link.kind
        ]
        response = await self._call(method, link.item_id)

        if not isinstance(response, dict) or response.get("success") is False:
            reason = response.get("error") if isinstance(response, dict) else ""
            raise SpotiflacError(
                ErrorKind.TRACK_NOT_FOUND,
                f"{self.site.label} returned nothing for this {link.kind}"
                + (f": {reason}" if reason else "."),
                provider=f"ext:{self.ext_name}",
            )

        if link.kind == "track":
            item = (
                response.get("track")
                if isinstance(response.get("track"), dict)
                else response
            )
            track = await self._track_from_its_album(item) or to_track(item, self.site)
            tracks = [track] if track else []
            name = track.title if track else ""
            cover = track.cover_url if track else ""
        else:
            items = await self._with_titles(response.get("tracks") or [])
            tracks = self._collection_tracks(response, link.kind, items)
            name = clean_album(
                _text(response.get("name") or response.get("title")),
                _text(response.get("artists")),
            )
            cover = _first_image(response)

        if tracks and self.match_isrcs:
            tracks = await attach_isrcs(tracks)
        if not name and tracks and link.kind == "album" and _known(tracks[0].album):
            # After the ISRC step, which fills an album name the page lacked.
            name = tracks[0].album
        return name, tracks, cover

    async def _with_titles(self, items: list) -> list:
        """`items`, with a title read from the track's own page for each one
        whose listing had none to give (Melon's title track reads "Title").

        One extension call per such track, so only for those: without it the
        track is dropped from the album, or saved under the badge's name.
        """
        out = []
        for item in items:
            if (
                isinstance(item, dict)
                and item.get("id")
                and not clean_title(_text(item.get("name") or item.get("title")))
            ):
                try:
                    page = await self._call("getTrack", str(item["id"]))
                    found = page.get("track") if isinstance(page, dict) else None
                    name = clean_title(_text((found or {}).get("name")))
                    if name:
                        item = {**item, "name": name}
                except Exception as exc:
                    logger.debug("[catalogue] title for %s: %s", item.get("id"), exc)
            out.append(item)
        return out

    def _collection_tracks(
        self, response: dict, kind: str, items: list | None = None
    ) -> list[TrackMetadata]:
        collection = {**response, "type": kind}
        if items is None:
            items = response.get("tracks") or []
        return [
            t
            for position, item in enumerate(items, start=1)
            if (
                t := to_track(
                    item,
                    self.site,
                    position=position,
                    total=len(items),
                    collection=collection,
                )
            )
        ]

    async def _track_from_its_album(self, item: dict) -> TrackMetadata | None:
        """The track as its album lists it, when the album does.

        A track page is where these extensions scrape worst — Bugs' yields
        the site's "choose a player" notice as the title and no artist — while
        the album page lists the same track cleanly, with its number. The
        album is only believed if it really contains this track: Melon's
        extension reports the song's own id as the album id.
        """
        if not isinstance(item, dict):
            return None
        raw_id = str(item.get("id") or "").split(":")[-1]
        album_id = str(item.get("album_id") or "")
        if not raw_id or not album_id or album_id == raw_id:
            return None
        try:
            album = await self._call("getAlbum", album_id)
        except Exception as exc:
            logger.debug("[catalogue] album %s for track %s: %s", album_id, raw_id, exc)
            return None
        if not isinstance(album, dict):
            return None
        for track in self._collection_tracks(album, "album"):
            if track.id.split(":")[-1] != raw_id:
                continue
            # The track page names the album better than an album page
            # whose heading an extension misread.
            page_album = clean_album(_text(item.get("album_name")), track.artists)
            if page_album and not _known(track.album):
                track = track.model_copy(update={"album": page_album})
            return track
        return None

    def get_url(self, url: str, include_featuring: bool = True, **kwargs: Any):
        return asyncio.run(self.get_url_async(url, include_featuring, **kwargs))

    # -- search -----------------------------------------------------------------

    async def search_async(self, query: str, limit: int = 50) -> dict[str, list]:
        """Tracks the extension finds, plus the albums they come from.

        No ISRC matching here: that is a Spotify search per result, and a
        result list is browsed far more often than it is downloaded — the
        link a result opens is matched when it is fetched.
        """
        # The limit as a number: the mobile contract is
        # `searchTracks(query, limit)`, and apple-music passes what it gets
        # straight into its API request, which answers an object with a 400.
        response = await self._call("searchTracks", query, limit)
        if isinstance(response, dict):
            items = response.get("tracks") or response.get("items") or []
        else:
            items = response or []

        tracks = [t for item in items if (t := to_track(item, self.site))][:limit]

        albums: list[dict] = []
        seen: set[str] = set()
        for track in tracks:
            if not track.album_id or track.album_id in seen:
                continue
            seen.add(track.album_id)
            albums.append(
                {
                    "id": track.album_id,
                    "name": track.album,
                    "artists": track.album_artist or track.artists,
                    "cover_url": track.cover_url,
                    "external_url": track.album_url,
                }
            )
        return {"tracks": tracks, "albums": albums, "artists": [], "playlists": []}

    def search(self, query: str, limit: int = 50) -> dict[str, list]:
        return asyncio.run(self.search_async(query, limit))
