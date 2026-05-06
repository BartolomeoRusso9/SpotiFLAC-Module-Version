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
from urllib.parse import urlparse

import requests

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

    def get_album_tracks(self, album_id: str) -> tuple[dict, list[TrackMetadata]]:
        album = self._get(f"/albums/{album_id}")
        items = self._paginate(f"/albums/{album_id}/tracks")
        tracks = [self._track_from_album_item(item, album) for item in items]
        return album, tracks

    # ------------------------------------------------------------------
    # Fetch playlist completa (con paginazione)
    # ------------------------------------------------------------------

    def get_playlist_tracks(self, playlist_uuid: str) -> tuple[dict, list[TrackMetadata]]:
        """
        Recupera tutti i metadati di una playlist Tidal.

        Le playlist usano UUID come identificatori (non interi come tracce/album).
        L'endpoint /playlists/{uuid}/tracks restituisce oggetti con un campo
        "item" che contiene i dati della traccia vera e propria.
        """
        playlist  = self._get(f"/playlists/{playlist_uuid}")
        raw_items = self._paginate(f"/playlists/{playlist_uuid}/tracks")

        tracks: list[TrackMetadata] = []
        for entry in raw_items:
            # Le playlist Tidal wrappano la traccia nel campo "item"
            track_data = entry.get("item") or entry
            if not track_data or not track_data.get("id"):
                continue

            # Salta tracce non disponibili nel catalogo (rimosse o geo-bloccate)
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
            include_groups: str = "ALBUM,EP,SINGLE",
    ) -> tuple[dict, list[TrackMetadata]]:
        """
        Recupera la discografia completa di un artista Tidal.
        include_groups: ALBUM, EP, SINGLE, COMPILATION (separati da virgola).
        Deduplica automaticamente tramite ISRC.
        """
        artist = self._get(f"/artists/{artist_id}")
        tracks: list[TrackMetadata] = []
        seen_isrc: set[str] = set()
        seen_album_ids: set[str] = set()

        for group in include_groups.split(","):
            group = group.strip().upper()
            try:
                albums = self._paginate(
                    f"/artists/{artist_id}/albums",
                    extra_params={"filter": group},
                )
            except Exception as exc:
                logger.warning("[tidal_metadata] gruppo %s fallito: %s", group, exc)
                continue

            for album_data in albums:
                album_id = str(album_data.get("id", ""))
                if not album_id or album_id in seen_album_ids:
                    continue
                seen_album_ids.add(album_id)

                try:
                    _, album_tracks = self.get_album_tracks(album_id)
                    for track in album_tracks:
                        if track.isrc and track.isrc in seen_isrc:
                            logger.debug(
                                "[tidal_metadata] duplicato saltato: %s (ISRC %s)",
                                track.title, track.isrc,
                            )
                            continue
                        if track.isrc:
                            seen_isrc.add(track.isrc)
                        tracks.append(track)
                except Exception as exc:
                    logger.warning(
                        "[tidal_metadata] album %s saltato: %s", album_id, exc
                    )

                time.sleep(0.3)  # rate limit tra album

            time.sleep(0.5)  # rate limit tra gruppi

        return artist, tracks

    # ------------------------------------------------------------------
    # Entry point pubblico
    # ------------------------------------------------------------------

    def get_url(self, tidal_url: str) -> tuple[str, list[TrackMetadata]]:
        """
        Riceve un URL Tidal e restituisce (nome_collezione, [TrackMetadata]).
        Equivalente a SpotifyMetadataClient.get_url() ma per Tidal.
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
            group_map = {
                "albums":       "ALBUM",
                "eps":          "EP",
                "singles":      "SINGLE",
                "compilations": "COMPILATION",
                "all":          "ALBUM,EP,SINGLE,COMPILATION",
            }
            raw_group      = info.get("group", "all")
            include_groups = group_map.get(raw_group, "ALBUM,EP,SINGLE")
            artist, tracks = self.get_artist_albums(info["id"], include_groups)
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
        Formato: https://resources.tidal.com/images/{uuid_con_slash}/1280x1280.jpg
        """
        cover = album.get("cover", "")
        if not cover:
            return ""
        return f"https://resources.tidal.com/images/{cover.replace('-', '/')}/1280x1280.jpg"

    def _fetch_album_details(self, album_id: int | str) -> dict:
        """Recupera i dettagli completi dell'album con gestione errori silenziosa."""
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

        fetch_album_details=True  → usato per tracce singole: fa una GET
                                    separata sull'album per avere cover HD,
                                    data di rilascio e numero tracce preciso.
        fetch_album_details=False → usato per playlist: usa solo i dati già
                                    presenti nell'oggetto traccia, evitando
                                    N richieste HTTP aggiuntive.
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
            duration_ms  = int(data.get("duration", 0)) * 1000,  # Tidal usa secondi
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