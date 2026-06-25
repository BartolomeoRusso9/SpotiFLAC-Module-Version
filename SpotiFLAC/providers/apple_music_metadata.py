"""
AppleMusicMetadataClient — retrieves metadati di tracks/album/artisti/playlist
tramite la AMP API pubblica di Apple Music.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time as _time
import unicodedata
import urllib.parse
from typing import Any

from ..core.errors import AuthError, ErrorKind, InvalidUrlError, SpotiflacError
from ..core.http import AsyncHttpClient
from ..core.models import TrackMetadata

logger = logging.getLogger(__name__)

_APPLE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

_JWT_KNOWN_PREFIXES = (
    "eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCIsImtpZCI6IldlYlBsYXlLaWQifQ.",
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJFUzI1NiIsImtpZCI6IldlYlBsYXlLaWQifQ.",
)
_JWT_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-."
)


def _extract_jwt_from_string(text: str) -> str | None:
    """
    Estrae un token JWT Apple Music da una stringa usando i prefissi noti
    (indexOf + scan carattere per carattere, come extractJWTFromString in index.js).
    """
    for prefix in _JWT_KNOWN_PREFIXES:
        idx = text.find(prefix)
        if idx == -1:
            continue
        end = idx
        while end < len(text) and text[end] in _JWT_CHARS:
            end += 1
        candidate = text[idx:end]
        parts = candidate.split(".")
        if len(parts) == 3 and all(parts):
            return candidate
    return None


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------


def is_apple_music_url(url: str) -> bool:
    return "music.apple.com" in url.lower()


def parse_apple_music_url(url: str) -> dict[str, str]:
    """
    Parse un URL Apple Music e restituisce type, id e storefront.
    """
    url = (url or "").strip()

    m = re.search(
        r"music\.apple\.com/([a-z]{2})/(album|playlist|artist|song)/[^/]*/([a-zA-Z0-9.]+)",
        url,
        re.IGNORECASE,
    )
    if not m:
        raise InvalidUrlError(f"Apple Music URL not recognized: {url}")

    storefront = m.group(1).lower()
    kind = m.group(2).lower()
    entity_id = m.group(3)

    song_m = re.search(r"[?&]i=(\d+)", url)
    if kind == "album" and song_m:
        return {"type": "track", "id": song_m.group(1), "storefront": storefront}
    if kind == "song":
        return {"type": "track", "id": entity_id, "storefront": storefront}
    if kind == "album":
        return {"type": "album", "id": entity_id, "storefront": storefront}
    if kind == "playlist":
        return {"type": "playlist", "id": entity_id, "storefront": storefront}
    if kind == "artist":
        return {"type": "artist", "id": entity_id, "storefront": storefront}

    raise InvalidUrlError(url)


# ---------------------------------------------------------------------------
# Helper normalizzazione
# ---------------------------------------------------------------------------


def _normalize_artist(s: str) -> str:
    s = s.lower().strip()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _artist_in_track(artist_name: str, track_artists: str) -> bool:
    name_norm = _normalize_artist(artist_name)
    for artist in track_artists.split(","):
        if _normalize_artist(artist) == name_norm:
            return True
    return False


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class AppleMusicMetadataClient:
    def __init__(self, timeout_s: int = 15) -> None:
        self._timeout = timeout_s
        self._http = AsyncHttpClient(
            provider="apple_metadata",
            timeout_s=timeout_s,
            headers={
                "User-Agent": _APPLE_UA,
                "Accept": "application/json",
                "Origin": "https://music.apple.com",
                "Referer": "https://music.apple.com/",
            },
        )
        self._auth_token: str | None = None
        self._token_expiry: float = 0.0  # timestamp Unix; 0 = mai valido

    async def __aenter__(self) -> "AppleMusicMetadataClient":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass  # The HTTP client's lifecycle is managed by NetworkManager

    # ------------------------------------------------------------------
    # Token handling
    # ------------------------------------------------------------------

    def _parse_token_expiry(self, token: str) -> None:
        """Legge il campo `exp` dal payload JWT e imposta la scadenza interna."""
        try:
            payload_b64 = token.split(".")[1]
            padded = payload_b64 + "=" * (-len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded))
            if "exp" in payload:
                self._token_expiry = float(payload["exp"]) - 300.0
            else:
                self._token_expiry = _time.time() + 43200.0
        except Exception:
            self._token_expiry = _time.time() + 43200.0

    async def _get_token(self) -> str:
        """
        Estrae il token JWT anonimo dal frontend web usando 3 strategie:
        1. devToken=JWT nel sorgente HTML
        2. Prefissi JWT noti nell'HTML
        3. Bundle JS della pagina (saltando quelli legacy)
        """
        if self._auth_token and _time.time() < self._token_expiry:
            return self._auth_token

        try:
            res = await self._http.get(
                "https://music.apple.com/us/browse", timeout=self._timeout
            )
            html = res.text
            unquoted_html = urllib.parse.unquote(html)

            # Strategia 1: devToken=JWT nel parametro URL
            m = re.search(
                r"devToken=([A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)",
                html,
            )
            if m:
                token = m.group(1)
                self._auth_token = token
                self._parse_token_expiry(token)
                return token

            # Strategia 2: Prefissi JWT noti nell'HTML
            token = _extract_jwt_from_string(unquoted_html)
            if token:
                self._auth_token = token
                self._parse_token_expiry(token)
                return token

            # Strategia 3: Bundle JS (salto quelli legacy)
            js_scripts = re.findall(r'src="(/assets/index[^"]*\.js)"', html)
            if not js_scripts:
                js_scripts = re.findall(r'src="(/assets/[^"]*\.js)"', html)

            for src in js_scripts[:6]:
                if "-legacy" in src:
                    continue
                js_url = "https://music.apple.com" + src
                try:
                    js_res = await self._http.get(js_url, timeout=self._timeout)
                    token = _extract_jwt_from_string(urllib.parse.unquote(js_res.text))
                    if token:
                        logger.debug(
                            "[apple_metadata] Token found in JS bundle: %s", src
                        )
                        self._auth_token = token
                        self._parse_token_expiry(token)
                        return token
                except Exception:
                    continue

            raise SpotiflacError(
                ErrorKind.NETWORK_ERROR,
                "JWT token not found in HTML or JS bundles.",
            )

        except SpotiflacError:
            raise
        except Exception as e:
            logger.error("[apple_metadata] Unable to retrieve JWT token: %s", e)
            raise SpotiflacError(
                ErrorKind.NETWORK_ERROR,
                f"Unable to retrieve Apple Music token: {e}",
            )

    async def _get(
        self, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}"}

        url = (
            path
            if path.startswith("https://")
            else f"https://amp-api.music.apple.com/v1/catalog/{path.lstrip('/')}"
        )

        try:
            resp = await self._http.get(
                url, params=params, headers=headers, timeout=self._timeout
            )
            return resp.json()
        except AuthError:
            # Token scaduto: forza rinnovo e riprova una volta
            self._auth_token = None
            self._token_expiry = 0.0
            token = await self._get_token()
            headers = {"Authorization": f"Bearer {token}"}
            resp = await self._http.get(
                url, params=params, headers=headers, timeout=self._timeout
            )
            return resp.json()

    # ------------------------------------------------------------------
    # Paginazione
    # ------------------------------------------------------------------

    async def _pagete_tracks(
        self,
        initial_items: list[dict[str, Any]],
        first_next: str | None,
        label: str = "resource",
    ) -> list[dict[str, Any]]:
        """Completa la lista delle tracks seguendo i link `next` successivi."""
        items = list(initial_items)
        next_path = first_next

        while next_path:
            try:
                page = await self._get(f"https://amp-api.music.apple.com{next_path}")
                page_items = page.get("data", [])
                if not page_items:
                    break
                items.extend(page_items)
                next_path = page.get("next")
                await asyncio.sleep(0.3)
            except Exception as exc:
                logger.warning(
                    "[apple_metadata] Track pagination %s interrupted: %s", label, exc
                )
                break

        return items

    async def _pagete_relationship(self, initial_path: str) -> list[dict[str, Any]]:
        """Itera una relazione standalone (es. /artists/{id}/albums) seguendo `next`."""
        results: list[dict[str, Any]] = []
        next_url: str | None = initial_path

        while next_url:
            try:
                data = await self._get(next_url)
            except Exception as exc:
                logger.warning("[apple_metadata] Pagination interrupted: %s", exc)
                break

            page = data.get("data", [])
            results.extend(page)

            raw_next = data.get("next")
            if not raw_next or not page:
                break

            next_url = f"https://amp-api.music.apple.com{raw_next}"
            await asyncio.sleep(0.3)

        return results

    # ------------------------------------------------------------------
    # Metodi di fetching
    # ------------------------------------------------------------------

    async def get_track(self, track_id: str, storefront: str = "us") -> TrackMetadata:
        data = await self._get(
            f"/{storefront}/songs/{track_id}",
            {"include": "albums", "extend": "editorialArtwork"},
        )
        results = data.get("data", [])
        if not results:
            raise SpotiflacError(
                ErrorKind.TRACK_NOT_FOUND, f"Track {track_id} not found."
            )
        return self._parse_item(results[0])

    async def get_album_tracks(
        self,
        album_id: str,
        storefront: str = "us",
    ) -> tuple[dict[str, Any], list[TrackMetadata]]:
        data = await self._get(
            f"/{storefront}/albums/{album_id}",
            {"include": "tracks,artists", "extend": "editorialArtwork"},
        )
        results = data.get("data", [])
        if not results:
            raise SpotiflacError(
                ErrorKind.TRACK_NOT_FOUND, f"Album {album_id} not found."
            )

        album_data = results[0]
        tracks_rel = album_data.get("relationships", {}).get("tracks", {})

        tracks_items = await self._pagete_tracks(
            initial_items=tracks_rel.get("data", []),
            first_next=tracks_rel.get("next"),
            label=f"album {album_id}",
        )

        tracks = [self._parse_item(item, album_data) for item in tracks_items]

        album_attr = album_data.get("attributes", {})
        artwork_url = (
            album_attr.get("artwork", {}).get("url", "").replace("{w}x{h}", "3000x3000")
        )
        release_date = album_attr.get("releaseDate", "").split("T")[0]

        formatted_album = {
            "attributes": {
                "name": album_attr.get("name", "Unknown"),
                "releaseDate": release_date,
                "artwork": {"url": artwork_url},
                "trackCount": len(tracks),
            }
        }
        return formatted_album, tracks

    async def get_playlist_tracks(
        self,
        playlist_id: str,
        storefront: str = "us",
    ) -> tuple[dict[str, Any], list[TrackMetadata]]:
        data = await self._get(
            f"/{storefront}/playlists/{playlist_id}",
            {"include": "tracks", "extend": "editorialArtwork"},
        )
        results = data.get("data", [])
        if not results:
            raise SpotiflacError(
                ErrorKind.TRACK_NOT_FOUND, f"Playlist {playlist_id} not found."
            )

        playlist_data = results[0]
        tracks_rel = playlist_data.get("relationships", {}).get("tracks", {})

        tracks_items = await self._pagete_tracks(
            initial_items=tracks_rel.get("data", []),
            first_next=tracks_rel.get("next"),
            label=f"playlist {playlist_id}",
        )

        tracks = [
            self._parse_item(item)
            for item in tracks_items
            if item.get("type") == "songs"
        ]
        return playlist_data, tracks

    async def get_artist_albums(
        self,
        artist_id: str,
        include_featuring: bool = False,
        storefront: str = "us",
    ) -> tuple[dict[str, Any], list[TrackMetadata]]:
        artist_data = await self._get(f"/{storefront}/artists/{artist_id}")
        artist_results = artist_data.get("data", [])
        if not artist_results:
            raise SpotiflacError(
                ErrorKind.TRACK_NOT_FOUND, f"Artist {artist_id} not found."
            )

        artist_obj = artist_results[0]
        artist_name = artist_obj.get("attributes", {}).get("name", "Unknown")

        album_ids: list[str] = []
        seen_ids: set[str] = set()

        for album_data in await self._pagete_relationship(
            f"/{storefront}/artists/{artist_id}/albums"
        ):
            aid = str(album_data.get("id", ""))
            if aid and aid not in seen_ids:
                seen_ids.add(aid)
                album_ids.append(aid)

        own_album_ids: set[str] = set(album_ids)

        if include_featuring:
            for album_data in await self._pagete_relationship(
                f"/{storefront}/artists/{artist_id}/appears-on-albums"
            ):
                aid = str(album_data.get("id", ""))
                if aid and aid not in seen_ids:
                    seen_ids.add(aid)
                    album_ids.append(aid)

        logger.info(
            "[apple_metadata] %s: %d album totali da scaricare",
            artist_name,
            len(album_ids),
        )

        # Fetch parallelo con asyncio.gather
        async def _fetch_one(
            aid: str,
        ) -> tuple[str, list[TrackMetadata] | None]:
            try:
                _, album_tracks = await self.get_album_tracks(
                    aid, storefront=storefront
                )
                return aid, album_tracks
            except Exception as exc:
                logger.warning("[apple_metadata] Album %s skipped: %s", aid, exc)
                return aid, None

        raw_results = await asyncio.gather(*[_fetch_one(aid) for aid in album_ids])

        results_dict: dict[str, list[TrackMetadata]] = {
            aid: tracks for aid, tracks in raw_results if tracks is not None
        }

        tracks: list[TrackMetadata] = []
        seen_isrc: set[str] = set()

        for aid in album_ids:
            if aid not in results_dict:
                continue
            for track in results_dict[aid]:
                if track.isrc and track.isrc in seen_isrc:
                    continue
                if include_featuring and aid not in own_album_ids:
                    if not _artist_in_track(artist_name, track.artists):
                        continue
                if track.isrc:
                    seen_isrc.add(track.isrc)
                tracks.append(track)

        return artist_obj, tracks

    # ------------------------------------------------------------------
    # Entry point pubblico
    # ------------------------------------------------------------------

    async def get_url(
        self,
        url: str,
        include_featuring: bool = False,
    ) -> tuple[str, list[TrackMetadata], str, dict[str, Any]]:
        info = parse_apple_music_url(url)
        t = info["type"]
        storefront = info.get("storefront", "us")

        if t == "track":
            meta = await self.get_track(info["id"], storefront=storefront)
            return meta.title, [meta], meta.cover_url, {}

        if t == "album":
            album, tracks = await self.get_album_tracks(
                info["id"], storefront=storefront
            )
            name = album.get("attributes", {}).get("name", "Unknown Album")
            release_date = album.get("attributes", {}).get("releaseDate", "")
            artwork_url = (
                album.get("attributes", {})
                .get("artwork", {})
                .get("url", "")
                .replace("{w}x{h}", "3000x3000")
            )
            album_meta = {"release_date": release_date, "track_count": len(tracks)}
            return name, tracks, artwork_url, album_meta

        if t == "playlist":
            playlist, tracks = await self.get_playlist_tracks(
                info["id"], storefront=storefront
            )
            name = playlist.get("attributes", {}).get("name", "Unknown Playlist")
            artwork_url = (
                playlist.get("attributes", {})
                .get("artwork", {})
                .get("url", "")
                .replace("{w}x{h}", "3000x3000")
            )
            return name, tracks, artwork_url, {}

        if t == "artist":
            artist, tracks = await self.get_artist_albums(
                info["id"],
                include_featuring=include_featuring,
                storefront=storefront,
            )
            name = artist.get("attributes", {}).get("name", "Unknown Artist")
            artwork_url = (
                artist.get("attributes", {})
                .get("artwork", {})
                .get("url", "")
                .replace("{w}x{h}", "3000x3000")
            )
            return name, tracks, artwork_url, {}

        raise SpotiflacError(
            ErrorKind.INVALID_URL,
            f"Apple Music type not supported: {t} (supportati: track, album, playlist, artist)",
        )

    # ------------------------------------------------------------------
    # Conversione dati API → TrackMetadata
    # ------------------------------------------------------------------

    def _parse_item(
        self,
        item: dict[str, Any],
        parent_album: dict[str, Any] | None = None,
    ) -> TrackMetadata:
        attr = item.get("attributes", {})
        album_attr = parent_album.get("attributes", {}) if parent_album else {}

        artwork_dict = attr.get("artwork", {})
        cover_url = artwork_dict.get("url", "").replace("{w}x{h}", "3000x3000")
        if not cover_url and parent_album:
            cover_url = (
                album_attr.get("artwork", {})
                .get("url", "")
                .replace("{w}x{h}", "3000x3000")
            )

        release_date = attr.get("releaseDate", "").split("T")[0]

        genre_names: list[str] = attr.get("genreNames") or []
        genre = ", ".join(g for g in genre_names if g != "Music")

        return TrackMetadata(
            id=f"apple_{item.get('id', '')}",
            title=attr.get("name", "Unknown"),
            artists=attr.get("artistName", "Unknown"),
            album=attr.get("albumName", album_attr.get("name", "Unknown")),
            album_artist=album_attr.get(
                "artistName", attr.get("artistName", "Unknown")
            ),
            isrc=attr.get("isrc", ""),
            track_number=attr.get("trackNumber", 1),
            disc_number=attr.get("discNumber", 1),
            duration_ms=attr.get("durationInMillis", 0),
            release_date=release_date,
            cover_url=cover_url,
            external_url=attr.get("url", ""),
            genre=genre,
            label=album_attr.get("recordLabel", ""),
            copyright=album_attr.get("copyright", ""),
            composer=attr.get("composerName", ""),
        )
