"""
TidalMetadataClient — recupera metadati di tracce/album/playlist/artisti direttamente
dall'API pubblica di Tidal quando l'URL di input è un link Tidal (non Spotify).

URL supportati:
  - https://listen.tidal.com/track/12345678
  - https://tidal.com/browse/track/12345678
  - https://listen.tidal.com/album/12345678
  - https://tidal.com/browse/album/12345678
  - https://listen.tidal.com/playlist/a1b2c3d4-e5f6-7890-abcd-ef1234567890
  - https://tidal.com/browse/playlist/a1b2c3d4-e5f6-7890-abcd-ef1234567890
  - https://listen.tidal.com/artist/12345678
  - https://tidal.com/browse/artist/12345678
  - https://listen.tidal.com/artist/12345678/discography/albums
  - https://listen.tidal.com/artist/12345678/discography/singles

L'ID della traccia viene inserito nel campo `TrackMetadata.id` con il prefisso
"tidal_" (es. "tidal_12345678") in modo che TidalProvider possa riconoscerlo
e saltare la fase di risoluzione Spotify→Tidal.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests
import unicodedata
import re

from ..core.errors import AuthError, NetworkError, InvalidUrlError, SpotiflacError, ErrorKind
from ..core.models import TrackMetadata

logger = logging.getLogger(__name__)

_TIDAL_CLIENT_ID = "CzET4vdadNUFQ5JU"
_TIDAL_API_BASE  = "https://api.tidal.com/v1"
_TIDAL_COUNTRY   = "US"
_TIDAL_UA        = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

_TIDAL_DOMAINS = {"listen.tidal.com", "tidal.com", "www.tidal.com"}

# Dimensione pagina per le richieste paginate (max consentito dall'API Tidal)
_PAGE_SIZE = 100

# Valori validi per il parametro filter dell'endpoint /artists/{id}/albums
# FIX: "ALBUMS" senza filter restituisce tutti i tipi — va sempre passato esplicitamente.
_TIDAL_FILTER_ALBUMS       = "ALBUMS"
_TIDAL_FILTER_EPSANDSINGLES = "EPSANDSINGLES"
_TIDAL_FILTER_COMPILATIONS  = "COMPILATIONS"


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

def is_tidal_url(url: str) -> bool:
    """Restituisce True se l'URL appartiene a Tidal."""
    try:
        return urlparse(url).netloc in _TIDAL_DOMAINS
    except Exception:
        return False


def parse_tidal_url(url: str) -> dict[str, str]:
    """
    Analizza un URL Tidal e restituisce {"type": ..., "id": ...}.

    Tipi supportati: "track", "album", "playlist", "artist", "artist_discography".
    Raises InvalidUrlError se il formato non è riconoscuto.
    """
    u    = urlparse(url)
    path = u.path.strip("/")

    if path.startswith("browse/"):
        path = path[len("browse/"):]

    parts = [p for p in path.split("/") if p]

    if len(parts) >= 2 and parts[0] in ("track", "album", "playlist", "artist"):
        entity_type = parts[0]
        entity_id   = parts[1].split("?")[0]

        # https://listen.tidal.com/artist/123/discography/albums
        if entity_type == "artist" and len(parts) >= 3 and parts[2] == "discography":
            group = parts[3] if len(parts) >= 4 else "all"
            return {"type": "artist_discography", "id": entity_id, "group": group}

        return {"type": entity_type, "id": entity_id}

    raise InvalidUrlError(url)


# ---------------------------------------------------------------------------
# Helper
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

class TidalMetadataClient:
    """
    Recupera metadati dall'API pubblica di Tidal v1.
    Non richiede credenziali utente — usa solo il client token pubblico.
    """

    def __init__(self, timeout_s: int = 15) -> None:
        self._timeout = timeout_s
        self._session = requests.Session()
        self._session.headers.update({
            "X-Tidal-Token": _TIDAL_CLIENT_ID,
            "Accept":        "application/json",
            "User-Agent":    _TIDAL_UA,
        })

    # ------------------------------------------------------------------
    # HTTP helper
    # ------------------------------------------------------------------

    def _get(self, path: str, extra_params: dict | None = None) -> dict:
        params = {"countryCode": _TIDAL_COUNTRY}
        if extra_params:
            params.update(extra_params)

        resp = self._session.get(
            f"{_TIDAL_API_BASE}/{path.lstrip('/')}",
            params  = params,
            timeout = self._timeout,
        )

        if resp.status_code == 401:
            raise AuthError("tidal_metadata", "Token Tidal non valido o scaduto")
        if resp.status_code == 404:
            raise SpotiflacError(
                ErrorKind.TRACK_NOT_FOUND,
                f"Risorsa non trovata: {path}",
                "tidal_metadata",
            )
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 5)) + 1
            logger.warning("[tidal_metadata] Rate limited — attendo %ds", wait)
            time.sleep(wait)
            return self._get(path, extra_params)
        if resp.status_code != 200:
            raise NetworkError("tidal_metadata", f"HTTP {resp.status_code} da {path}")

        return resp.json()

    # ------------------------------------------------------------------
    # Paginazione generica
    # ------------------------------------------------------------------

    def _paginate(self, path: str, extra_params: dict | None = None) -> list[dict]:
        """
        Recupera tutti gli elementi di un endpoint paginato Tidal.
        Gestisce automaticamente offset e totalNumberOfItems.
        """
        items:  list[dict] = []
        offset: int        = 0

        while True:
            params = {"limit": _PAGE_SIZE, "offset": offset}
            if extra_params:
                params.update(extra_params)

            data  = self._get(path, params)
            page  = data.get("items", [])
            total = data.get("totalNumberOfItems", len(page))

            items.extend(page)
            offset += len(page)

            logger.debug(
                "[tidal_metadata] paginazione %s: %d/%d", path, offset, total
            )

            if offset >= total or not page:
                break

            time.sleep(0.3)  # rispetta il rate limit

        return items

    # ------------------------------------------------------------------
    # Fetch singola traccia
    # ------------------------------------------------------------------

    def get_track(self, track_id: str) -> TrackMetadata:
        data = self._get(f"/tracks/{track_id}")
        return self._track_from_raw(data)

    # ------------------------------------------------------------------
    # Fetch album completo
    # ------------------------------------------------------------------

    def get_album_tracks(
            self,
            album_id: str,
            preloaded_album: dict | None = None,
    ) -> tuple[dict, list[TrackMetadata]]:
        """
        Recupera tutte le tracce di un album.
        Se preloaded_album è fornito (dati già disponibili), evita una chiamata HTTP aggiuntiva.
        """
        album = preloaded_album if preloaded_album else self._get(f"/albums/{album_id}")
        items = self._paginate(f"/albums/{album_id}/tracks")
        tracks = [self._track_from_album_item(item, album) for item in items]
        return album, tracks

    # ------------------------------------------------------------------
    # Fetch playlist completa (con paginazione)
    # ------------------------------------------------------------------

    def get_playlist_tracks(self, playlist_uuid: str) -> tuple[dict, list[TrackMetadata]]:
        """
        Recupera tutti i metadati di una playlist Tidal.
        Le playlist usano UUID come identificatori.
        """
        playlist  = self._get(f"/playlists/{playlist_uuid}")
        raw_items = self._paginate(f"/playlists/{playlist_uuid}/tracks")

        tracks: list[TrackMetadata] = []
        for entry in raw_items:
            track_data = entry.get("item") or entry
            if not track_data or not track_data.get("id"):
                continue

            if track_data.get("streamReady") is False:
                logger.debug(
                    "[tidal_metadata] traccia non disponibile saltata: %s",
                    track_data.get("title", "?"),
                )
                continue

            tracks.append(self._track_from_raw(track_data, fetch_album_details=False))

        return playlist, tracks

    # ------------------------------------------------------------------
    # Fetch discografia artista
    # ------------------------------------------------------------------

    def get_artist_albums(
            self,
            artist_id: str,
            # FIX: default copre album + EP/singoli, NO compilazioni.
            # Le compilazioni contengono tracce di molti artisti e vanno abilitate
            # esplicitamente — analogia con "appears_on" di Spotify.
            include_groups: str = f"{_TIDAL_FILTER_ALBUMS},{_TIDAL_FILTER_EPSANDSINGLES}",
            include_featuring: bool = False,
    ) -> tuple[dict, list[TrackMetadata]]:
        """
        Recupera la discografia completa di un artista Tidal.

        include_groups: ALBUMS, EPSANDSINGLES, COMPILATIONS (separati da virgola).

        Logica featuring:
        - ALBUMS / EPSANDSINGLES → tutti i brani inclusi (release proprie)
        - COMPILATIONS           → solo le tracce dove l'artista compare effettivamente
                                   (evita di scaricare brani di altri presenti nella stessa compilation)

        FIX principale: il parametro `filter` viene SEMPRE passato esplicitamente.
        Senza filter, l'API Tidal restituisce tutti i tipi per default,
        ignorando il raggruppamento richiesto.
        """
        artist = self._get(f"/artists/{artist_id}")
        artist_name = artist.get("name", "")
        tracks: list[TrackMetadata] = []
        seen_isrc: set[str] = set()
        seen_album_ids: set[str] = set()

        # Raccogliamo tutti gli album da tutti i gruppi richiesti
        # Struttura: (album_id, preloaded_album_data, is_compilation)
        albums_to_fetch: list[tuple[str, dict, bool]] = []

        if include_featuring:
            existing = include_groups.split(",")
            if _TIDAL_FILTER_COMPILATIONS not in existing:
                existing.append(_TIDAL_FILTER_COMPILATIONS)
            include_groups = ",".join(existing)

        for group in include_groups.split(","):
            group = group.strip().upper()
            if not group:
                continue

            try:
                # FIX: passiamo SEMPRE filter, anche per ALBUMS.
                albums = self._paginate(
                    f"/artists/{artist_id}/albums",
                    extra_params={"filter": group},
                )
            except Exception as exc:
                logger.warning("[tidal_metadata] gruppo %s fallito: %s", group, exc)
                continue

            is_compilation = (group == _TIDAL_FILTER_COMPILATIONS)

            for album_data in albums:
                album_id = str(album_data.get("id", ""))
                if not album_id or album_id in seen_album_ids:
                    continue
                seen_album_ids.add(album_id)
                # Passiamo i dati dell'album già scaricati per evitare una chiamata
                # HTTP extra in get_album_tracks
                albums_to_fetch.append((album_id, album_data, is_compilation))

        # Fetch parallelo delle tracce di ogni album (max 5 richieste simultanee)
        with ThreadPoolExecutor(max_workers=5) as executor:
            future_to_album = {
                executor.submit(self.get_album_tracks, aid, preloaded): (aid, is_comp)
                for aid, preloaded, is_comp in albums_to_fetch
            }

            # Raccogliamo i risultati indicizzati per album_id
            results: dict[str, tuple[list, bool]] = {}
            for future in as_completed(future_to_album):
                album_id, is_compilation = future_to_album[future]
                try:
                    _, album_tracks = future.result()
                    results[album_id] = (album_tracks, is_compilation)
                except Exception as exc:
                    logger.warning("[tidal_metadata] album %s saltato: %s", album_id, exc)

        # Ricostruiamo in ordine originale (albums_to_fetch è ordinato)
        for album_id, _, is_compilation in albums_to_fetch:
            if album_id not in results:
                continue
            album_tracks, _ = results[album_id]
            for track in album_tracks:
                if track.isrc and track.isrc in seen_isrc:
                    logger.debug(
                        "[tidal_metadata] duplicato saltato: %s (ISRC %s)",
                        track.title, track.isrc,
                    )
                    continue
                if is_compilation and not _artist_in_track(artist_name, track.artists):
                    logger.debug(
                        "[tidal_metadata] traccia saltata (artista assente): %s — %s",
                        track.title, track.artists,
                    )
                    continue
                if track.isrc:
                    seen_isrc.add(track.isrc)
                tracks.append(track)
        return artist, tracks
    # ------------------------------------------------------------------
    # Entry point pubblico
    # ------------------------------------------------------------------

    def get_url(self, tidal_url: str, include_featuring: bool = False) -> tuple[str, list[TrackMetadata]]:
        """
        Riceve un URL Tidal e restituisce (nome_collezione, [TrackMetadata]).
        """
        info = parse_tidal_url(tidal_url)
        t    = info["type"]

        if t == "track":
            meta = self.get_track(info["id"])
            return meta.title, [meta]

        if t == "album":
            album, tracks = self.get_album_tracks(info["id"])
            return album.get("title", "Unknown Album"), tracks

        if t == "playlist":
            playlist, tracks = self.get_playlist_tracks(info["id"])
            return playlist.get("title", "Unknown Playlist"), tracks

        if t in ("artist", "artist_discography"):
            # Mappatura path URL → filtri API Tidal
            group_map = {
                "albums":       _TIDAL_FILTER_ALBUMS,
                "eps":          _TIDAL_FILTER_EPSANDSINGLES,
                "singles":      _TIDAL_FILTER_EPSANDSINGLES,
                "compilations": _TIDAL_FILTER_COMPILATIONS,
                "all":          f"{_TIDAL_FILTER_ALBUMS},{_TIDAL_FILTER_EPSANDSINGLES},{_TIDAL_FILTER_COMPILATIONS}",
            }
            raw_group      = info.get("group", "all")
            include_groups = group_map.get(
                raw_group,
                f"{_TIDAL_FILTER_ALBUMS},{_TIDAL_FILTER_EPSANDSINGLES}",
            )
            artist, tracks = self.get_artist_albums(
                info["id"],
                include_groups,
                include_featuring=include_featuring,
            )
            return artist.get("name", "Unknown Artist"), tracks

        raise SpotiflacError(
            ErrorKind.INVALID_URL,
            f"Tipo Tidal non supportato: {t} (supportati: track, album, playlist, artist)",
        )

    # ------------------------------------------------------------------
    # Conversione dati API → TrackMetadata
    # ------------------------------------------------------------------

    @staticmethod
    def _format_artists(artists: list[dict] | None) -> str:
        if not artists:
            return "Unknown"
        return ", ".join(a.get("name", "Unknown") for a in artists if a.get("name"))

    @staticmethod
    def _cover_url(album: dict) -> str:
        """
        Converte il campo cover di Tidal (UUID con trattini) nell'URL immagine HD.
        """
        cover = album.get("cover", "")
        if not cover:
            return ""
        return f"https://resources.tidal.com/images/{cover.replace('-', '/')}/1280x1280.jpg"

    def _fetch_album_details(self, album_id: int | str) -> dict:
        try:
            return self._get(f"/albums/{album_id}")
        except Exception as exc:
            logger.debug(
                "[tidal_metadata] impossibile recuperare album %s: %s", album_id, exc
            )
            return {}

    def _track_from_raw(
            self,
            data:                dict,
            fetch_album_details: bool = True,
    ) -> TrackMetadata:
        """
        Costruisce TrackMetadata da un oggetto traccia dell'API Tidal.

        fetch_album_details=True  → tracce singole: GET separata sull'album
                                    per cover HD, data e numero tracce preciso.
        fetch_album_details=False → playlist: usa solo i dati presenti,
                                    evita N richieste HTTP aggiuntive.
        """
        album   = data.get("album", {})
        artists = data.get("artists") or ([data["artist"]] if data.get("artist") else [])

        cover_url         = self._cover_url(album)
        release_date      = album.get("releaseDate", "")
        total_tracks      = album.get("numberOfTracks", 0)
        album_artists_raw = album.get("artists") or artists

        if fetch_album_details and album.get("id"):
            album_details = self._fetch_album_details(album["id"])
            if album_details:
                cover_url         = self._cover_url(album_details) or cover_url
                release_date      = album_details.get("releaseDate", release_date)
                total_tracks      = album_details.get("numberOfTracks", total_tracks)
                album_artists_raw = album_details.get("artists") or album_artists_raw

        return TrackMetadata(
            id           = f"tidal_{data.get('id', '')}",
            title        = data.get("title", "Unknown"),
            artists      = self._format_artists(artists),
            album        = album.get("title", "Unknown"),
            album_artist = self._format_artists(album_artists_raw),
            isrc         = data.get("isrc", ""),
            track_number = data.get("trackNumber", 0),
            disc_number  = data.get("volumeNumber", 1),
            total_tracks = total_tracks,
            duration_ms  = int(data.get("duration", 0)) * 1000,
            release_date = release_date,
            cover_url    = cover_url,
            external_url = data.get("url", ""),
        )

    def _track_from_album_item(self, data: dict, album: dict) -> TrackMetadata:
        """Costruisce TrackMetadata da un item in /albums/{id}/tracks."""
        artists = data.get("artists") or ([data["artist"]] if data.get("artist") else [])

        return TrackMetadata(
            id           = f"tidal_{data.get('id', '')}",
            title        = data.get("title", "Unknown"),
            artists      = self._format_artists(artists),
            album        = album.get("title", "Unknown"),
            album_artist = self._format_artists(album.get("artists") or artists),
            isrc         = data.get("isrc", ""),
            track_number = data.get("trackNumber", 0),
            disc_number  = data.get("volumeNumber", 1),
            total_tracks = album.get("numberOfTracks", 0),
            duration_ms  = int(data.get("duration", 0)) * 1000,
            release_date = album.get("releaseDate", ""),
            cover_url    = self._cover_url(album),
            external_url = data.get("url", ""),
        )