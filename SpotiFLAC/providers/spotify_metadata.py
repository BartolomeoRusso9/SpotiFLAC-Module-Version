from __future__ import annotations

import base64
import logging
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Iterator
from urllib.parse import urlparse, parse_qs

import requests

from ..core.errors import AuthError, NetworkError, InvalidUrlError, SpotiflacError, ErrorKind
from ..core.models import TrackMetadata
from ..core.isrc_cache import get_cached_isrc, put_cached_isrc

logger = logging.getLogger(__name__)

_CLIENT_ID     = base64.b64decode("ODNlNDQzMGI0NzAwNDM0YmFhMjEyMjhhOWM3ZDExYzU=").decode()
_CLIENT_SECRET = base64.b64decode("OWJiOWUxMzFmZjI4NDI0Y2I2YTQyMGFmZGY0MWQ0NGE=").decode()
_TOKEN_URL     = "https://accounts.spotify.com/api/token"
_API_BASE      = "https://api.spotify.com/v1"

# Regex per il preview mp3, identico a Go
_PREVIEW_RE    = re.compile(r"https://p\.scdn\.co/mp3-preview/[a-zA-Z0-9]+")
# Regex per i compositori nel JSON-LD dell'embed
_COMPOSER_RE   = re.compile(r'"composer"\s*:\s*\[([^\]]+)\]')
_NAME_RE       = re.compile(r'"name"\s*:\s*"([^"]+)"')

# Tipo del gruppo nell'album della discografia dell'artista
_FEATURING_GROUPS = frozenset({"appears_on", "compilation"})


# ---------------------------------------------------------------------------
# Nuovo dataclass (specchiato dal Go ArtistSimple)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ArtistSimple:
    """Artista con ID e URL esterno, per uso downstream (linking, dedup)."""
    id: str
    name: str
    external_url: str


# ---------------------------------------------------------------------------
# Parsing URL
# ---------------------------------------------------------------------------

def parse_spotify_url(uri: str) -> dict[str, str]:
    """
    Analizza un URL/URI Spotify restituendo {'type': ..., 'id': ...}.

    Solleva InvalidUrlError se l'URL non è riconoscibile (comportamento
    allineato al Go che restituisce errInvalidSpotifyURL).
    """
    u = urlparse(uri)

    # URL embed con ?uri=
    if u.netloc == "embed.spotify.com":
        qs = parse_qs(u.query)
        if not qs.get("uri"):
            raise InvalidUrlError(uri)
        return parse_spotify_url(qs["uri"][0])

    # URI nativo  spotify:track:xxx
    if u.scheme == "spotify":
        parts = uri.split(":")

    # URL web open.spotify.com / play.spotify.com
    elif u.netloc in ("open.spotify.com", "play.spotify.com"):
        parts = u.path.split("/")
        if len(parts) > 1 and parts[1] == "embed":
            parts = parts[1:]
        if len(parts) > 1 and parts[1].startswith("intl-"):
            parts = parts[1:]

    # ID nudo (22 caratteri alfanumerici): solo playlist
    elif not u.scheme and not u.netloc:
        path = u.path.strip()
        if re.match(r"^[A-Za-z0-9]{22}$", path):
            return {"type": "playlist", "id": path}
        raise InvalidUrlError(uri)

    else:
        raise InvalidUrlError(uri)

    if len(parts) == 3 and parts[1] in ("album", "track", "playlist", "artist"):
        return {"type": parts[1], "id": parts[2].split("?")[0]}
    if len(parts) == 5 and parts[3] == "playlist":
        return {"type": "playlist", "id": parts[4].split("?")[0]}
    if len(parts) >= 4 and parts[1] == "artist":
        dtype = "artist_discography" if parts[3] == "discography" else "artist"
        return {"type": dtype, "id": parts[2].split("?")[0]}

    raise InvalidUrlError(uri)


# ---------------------------------------------------------------------------
# Utilità
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
# Client principale
# ---------------------------------------------------------------------------

class SpotifyMetadataClient:
    def __init__(self, timeout_s: int = 10) -> None:
        self._timeout   = timeout_s
        self._session   = requests.Session()
        self._token     = ""
        self._token_exp = 0.0

    # ------------------------------------------------------------------ auth

    def _ensure_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token

        auth = base64.b64encode(f"{_CLIENT_ID}:{_CLIENT_SECRET}".encode()).decode()
        resp = self._session.post(
            _TOKEN_URL,
            headers={"Authorization": f"Basic {auth}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials"},
            timeout=self._timeout,
        )
        if resp.status_code != 200:
            raise AuthError("spotify", f"Token request failed: HTTP {resp.status_code}")

        body  = resp.json()
        token = body.get("access_token")
        if not token:
            raise AuthError("spotify", "No access_token in token response")

        self._token     = token
        self._token_exp = time.time() + body.get("expires_in", 3600)
        return self._token

    def _get(self, path: str, **kwargs) -> dict:
        """
        GET verso l'API REST di Spotify.

        Accetta sia path relativi ("/tracks/xxx") che URL assoluti
        (usati da _paginate per i link "next").
        """
        token = self._ensure_token()
        url   = (
            path
            if path.startswith("http")
            else f"{_API_BASE}/{path.lstrip('/')}"
        )

        for attempt in range(3):
            resp = self._session.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=self._timeout,
                **kwargs,
            )
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 5)) + 1
                logger.info("[spotify] rate limited — attendo %ss", retry_after)
                time.sleep(retry_after)
                continue
            if resp.status_code in (502, 503, 504) and attempt < 2:
                wait = 1.5 * (attempt + 1)
                logger.warning(
                    "[spotify] HTTP %s — retry %d/2 in %.1fs",
                    resp.status_code, attempt + 1, wait,
                )
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                raise NetworkError("spotify", f"HTTP {resp.status_code} da {path}")
            return resp.json()

        raise NetworkError("spotify", f"HTTP {resp.status_code} da {path} dopo i retry")

    def _paginate(self, url: str, delay: float = 0.5) -> Iterator[dict]:
        """Itera su tutte le pagine di un endpoint paginato."""
        while url:
            data  = self._get(url)
            yield from data.get("items", [])
            # "next" è un URL assoluto; strip dei parametri di locale
            url = (data.get("next") or "").split("&locale=")[0] or ""
            if url and delay > 0:
                time.sleep(delay)

    # ------------------------------------------------------------------ public

    def get_track(self, track_id: str, fetch_composer: bool = False) -> TrackMetadata:
        """
        Recupera i metadati di una singola traccia.

        fetch_composer=True aggiunge il compositore tramite scraping
        dell'embed page (chiamata HTTP aggiuntiva).
        """
        data = self._get(f"/tracks/{track_id}")
        meta = self._track_from_raw(data)

        if fetch_composer:
            try:
                composer = self._fetch_composer(track_id)
                if composer:
                    meta = self._replace(meta, composer=composer)
            except Exception as exc:
                logger.debug("[spotify] fetch composer fallito per %s: %s", track_id, exc)

        return meta

    def get_preview_url(self, track_id: str) -> str:
        """
        Scraping dell'embed page per ottenere l'URL mp3 di preview.

        Specchiato da Go GetPreviewURL(). Restituisce stringa vuota se
        il preview non è disponibile o la richiesta fallisce.
        """
        if not track_id:
            raise ValueError("track_id non può essere vuoto")

        url = f"https://open.spotify.com/embed/track/{track_id}"
        try:
            resp = self._session.get(url, timeout=15)
            if resp.status_code != 200:
                return ""
            match = _PREVIEW_RE.search(resp.text)
            return match.group(0) if match else ""
        except Exception as exc:
            logger.debug("[spotify] preview URL fallito per %s: %s", track_id, exc)
            return ""

    def get_album_tracks(self, album_id: str) -> tuple[dict, list[TrackMetadata]]:
        # --- TENTATIVO CON SOLUZIONE 2: GRAPHQL (Velocità Go-Level) ---
        try:
            from ..core.spotfetch import SpotifyWebClient
            logger.info(f"[spotify] Tentativo di recupero album {album_id} rapido tramite GraphQL...")
            
            web_client = SpotifyWebClient()
            web_client.initialize()
            
            all_items = []
            offset = 0
            limit = 1000
            album_data_gql = {}
            
            while True:
                payload = {
                    "operationName": "getAlbum",
                    "variables": {
                        "uri": f"spotify:album:{album_id}",
                        "locale": "",
                        "offset": offset,
                        "limit": limit
                    },
                    "extensions": {
                        "persistedQuery": {
                            "version": 1,
                            "sha256Hash": "b9bfabef66ed756e5e13f68a942deb60bd4125ec1f1be8cc42769dc0259b4b10"
                        }
                    }
                }
                
                response = web_client.query(payload)
                album_union = response.get("data", {}).get("albumUnion", {})
                
                if not album_data_gql:
                    album_data_gql = album_union
                    
                tracks_v2 = album_union.get("tracksV2", {})
                items = tracks_v2.get("items", [])
                
                if not items:
                    break
                    
                all_items.extend(items)
                
                total_count = tracks_v2.get("totalCount", 0)
                if len(all_items) >= total_count or len(items) < limit:
                    break
                    
                offset += limit
            
            if all_items:
                album_name = album_data_gql.get("name", "Unknown Album")
                album_url = f"https://open.spotify.com/album/{album_id}"
                
                cover_sources = album_data_gql.get("coverArt", {}).get("sources", [])
                cover_url = cover_sources[0].get("url", "") if cover_sources else ""
                
                album_artists_list = [a.get("profile", {}).get("name", "Unknown") for a in album_data_gql.get("artists", {}).get("items", [])]
                album_artist = ", ".join(album_artists_list) if album_artists_list else "Unknown Artist"
                
                publisher = album_data_gql.get("label", "")
                release_date = album_data_gql.get("date", {}).get("isoString", "")
                if release_date:
                    release_date = release_date[:10]
                    
                total_discs = album_data_gql.get("discs", {}).get("totalCount", 1)
                total_tracks = tracks_v2.get("totalCount", len(all_items))
                
                graphql_tracks = []
                for idx, item in enumerate(all_items):
                    track_data = item.get("track", {})
                    track_id = track_data.get("id", "")
                    track_uri = track_data.get("uri", "")
                    if not track_id and ":" in track_uri:
                        track_id = track_uri.split(":")[-1]
                        
                    if not track_id:
                        continue
                        
                    title = track_data.get("name", "Unknown")
                    
                    artists_list = [a.get("profile", {}).get("name", "Unknown") for a in track_data.get("artists", {}).get("items", [])]
                    artists_str = ", ".join(artists_list) if artists_list else album_artist
                    
                    duration_ms = track_data.get("duration", {}).get("totalMilliseconds", 0)
                    
                    # Generazione ArtistSimple interna
                    artists_data_list = []
                    for art_item in track_data.get("artists", {}).get("items", []):
                        a_uri = art_item.get("uri", "")
                        a_id = a_uri.split(":")[-1] if ":" in a_uri else ""
                        a_name = art_item.get("profile", {}).get("name", "")
                        if a_id:
                            artists_data_list.append(ArtistSimple(
                                id=a_id, 
                                name=a_name, 
                                external_url=f"https://open.spotify.com/artist/{a_id}"
                            ))

                    first_artist = artists_data_list[0] if artists_data_list else ArtistSimple("", "", "")
                    
                    meta = TrackMetadata(
                        id=track_id,
                        title=title,
                        artists=artists_str,
                        album=album_name,
                        album_artist=album_artist,
                        isrc="",  # Lasciamo fare la risoluzione veloce in bulk al downloader
                        track_number=track_data.get("trackNumber", idx + 1),
                        disc_number=track_data.get("discNumber", 1),
                        total_tracks=total_tracks,
                        duration_ms=duration_ms,
                        release_date=release_date,
                        cover_url=cover_url,
                        external_url=f"https://open.spotify.com/track/{track_id}",
                        copyright="",
                        composer="",
                        upc="",
                        publisher=publisher,
                        total_discs=total_discs,
                        album_type="album",
                        preview_url="",
                        album_id=album_id,
                        album_url=album_url,
                        artist_id=first_artist.id,
                        artist_url=first_artist.external_url,
                        artists_data=artists_data_list
                    )
                    graphql_tracks.append(meta)
                
                mock_album = {"name": album_name, "id": album_id, "label": publisher, "external_ids": {}}
                logger.info(f"[spotify] GraphQL completato. {len(graphql_tracks)} tracce album estratte.")
                return mock_album, graphql_tracks

        except Exception as exc:
            logger.warning(f"[spotify] GraphQL fallito su album ({exc}). Eseguo il fallback sulla REST API...")

        # --- FALLBACK: LOGICA RESTRITTIVA REST API ---
        album     = self._get(f"/albums/{album_id}")
        raw_items = list(self._paginate(f"{_API_BASE}/albums/{album_id}/tracks?limit=50"))

        upc       = (album.get("external_ids") or {}).get("upc", "")
        publisher = album.get("label", "")
        total_discs = max((item.get("disc_number", 1) for item in raw_items), default=1)

        isrc_map: dict[str, str] = {}
        missing: list[str] = []

        for item in raw_items:
            cached = get_cached_isrc(item["id"])
            if cached:
                isrc_map[item["id"]] = cached
            else:
                missing.append(item["id"])

        for i in range(0, len(missing), 50):
            chunk = missing[i : i + 50]
            try:
                data = self._get(f"/tracks?ids={','.join(chunk)}")
                for full_track in data.get("tracks", []):
                    if not full_track:
                        continue
                    tid  = full_track["id"]
                    isrc = full_track.get("external_ids", {}).get("isrc", "")
                    isrc_map[tid] = isrc
                    if isrc:
                        put_cached_isrc(tid, isrc)
            except Exception:
                pass

        tracks: list[TrackMetadata] = [
            self._track_from_album_item(
                item, album, isrc=isrc_map.get(item["id"], ""), upc=upc,
                publisher=publisher, total_discs=total_discs,
            ) for item in raw_items
        ]
        return album, tracks

    def get_playlist_tracks(self, playlist_id: str) -> tuple[dict, list[TrackMetadata]]:
        # --- TENTATIVO CON SOLUZIONE 2: GRAPHQL (Velocità Go-Level) ---
        try:
            from ..core.spotfetch import SpotifyWebClient
            logger.info("[spotify] Tentativo di recupero playlist rapido tramite GraphQL...")
            
            web_client = SpotifyWebClient()
            web_client.initialize()
            
            all_items = []
            offset = 0
            limit = 1000
            playlist_name = "Unknown Playlist"
            
            while True:
                payload = {
                    "operationName": "fetchPlaylist",
                    "variables": {
                        "uri": f"spotify:playlist:{playlist_id}",
                        "offset": offset,
                        "limit": limit,
                        "enableWatchFeedEntrypoint": False
                    },
                    "extensions": {
                        "persistedQuery": {
                            "version": 1,
                            "sha256Hash": "bb67e0af06e8d6f52b531f97468ee4acd44cd0f82b988e15c2ea47b1148efc77"
                        }
                    }
                }
                
                response = web_client.query(payload)
                playlist_v2 = response.get("data", {}).get("playlistV2", {})
                if not playlist_name or playlist_name == "Unknown Playlist":
                    playlist_name = playlist_v2.get("name", "Unknown Playlist")
                    
                content = playlist_v2.get("content", {})
                items = content.get("items", [])
                
                if not items:
                    break
                    
                all_items.extend(items)
                
                total_count = content.get("totalCount", 0)
                if len(all_items) >= total_count or len(items) < limit:
                    break
                    
                offset += limit
            
            if all_items:
                graphql_tracks = []
                for item in all_items:
                    track_data = item.get("itemV2", {}).get("data", {})
                    track_id = track_data.get("id", "")
                    track_uri = track_data.get("uri", "")
                    if not track_id and ":" in track_uri:
                        track_id = track_uri.split(":")[-1]
                        
                    if not track_id:
                        continue
                        
                    title = track_data.get("name", "Unknown")
                    raw_plays = track_data.get("playcount")
                    
                    # 2. Controlliamo se Spotify ci ha restituito un dizionario o un testo
                    if isinstance(raw_plays, dict):
                        plays_str = str(raw_plays.get("value") or raw_plays.get("total") or "0")
                    else:
                        plays_str = str(raw_plays or "0")

                    # 3. Estraiamo anche l'etichetta Explicit (E) come in Go
                    is_explicit = track_data.get("contentRating", {}).get("label") == "EXPLICIT"
                    # -------------------------------------
                    
                    # Estrazione e formattazione autori
                    artists_list = [a.get("profile", {}).get("name", "Unknown") for a in track_data.get("artists", {}).get("items", [])]
                    artists_str = ", ".join(artists_list) if artists_list else "Unknown Artist"
                    
                    # Estrazione info album
                    album_data = track_data.get("albumOfTrack", {})
                    album_name = album_data.get("name", "Unknown")
                    
                    album_artists_list = [a.get("profile", {}).get("name", "Unknown") for a in album_data.get("artists", {}).get("items", [])]
                    album_artist = ", ".join(album_artists_list) if album_artists_list else (artists_list[0] if artists_list else "Unknown Artist")
                    
                    # URL Copertina 
                    cover_sources = album_data.get("coverArt", {}).get("sources", [])
                    cover_url = cover_sources[0].get("url", "") if cover_sources else ""
                    
                    duration_ms = track_data.get("trackDuration", {}).get("totalMilliseconds", 0)
                    
                    # Se hai aggiornato il modello TrackMetadata con plays e is_explicit
                    meta = TrackMetadata(
                        id=track_id,
                        title=title,
                        artists=artists_str,
                        album=album_name,
                        album_artist=album_artist,
                        isrc="",
                        track_number=track_data.get("trackNumber", 0) or 0,
                        disc_number=1,
                        total_tracks=0,
                        duration_ms=duration_ms,
                        release_date="",
                        cover_url=cover_url,
                        external_url=f"https://open.spotify.com/track/{track_id}",
                        copyright="",
                        composer="",
                        plays=plays_str,           # <--- ORA E' SEMPRE UNA STRINGA PULITA (es. "1504200")
                        is_explicit=is_explicit    # <--- True o False
                    )
                    graphql_tracks.append(meta)
                
                mock_playlist = {
                    "name": playlist_name,
                    "id": playlist_id
                }
                logger.info(f"[spotify] GraphQL completato con successo! Estratte {len(graphql_tracks)} tracce all'istante.")
                return mock_playlist, graphql_tracks

        except Exception as exc:
            logger.warning(f"[spotify] GraphQL fallito ({exc}). Eseguo il fallback sulla REST API tradizionale...")

        # --- FALLBACK: LOGICA RESTRITTIVA REST API (Già Presente) ---
        playlist = self._get(f"/playlists/{playlist_id}")
        tracks: list[TrackMetadata] = []

        for item in self._paginate(f"{_API_BASE}/playlists/{playlist_id}/tracks?limit=100"):
            track = item.get("track")
            if not track or not track.get("id"):
                continue
            tracks.append(self._track_from_raw(track))

        return playlist, tracks

    def get_artist_albums(
            self,
            artist_id: str,
            include_groups: str = "album,single",
            include_featuring: bool = False,
    ) -> tuple[dict, list[TrackMetadata]]:
        """
        Recupera la discografia completa di un artista.

        Migliorie:
          - album_type ("album" | "single" | "appears_on" | "compilation")
            viene ora propagato a ogni TrackMetadata
          - struttura interna invariata (featuring filter, dedup ISRC, parallelo)
        """
        artist      = self._get(f"/artists/{artist_id}")
        artist_name = artist.get("name", "")
        tracks: list[TrackMetadata] = []
        seen_isrc:     set[str] = set()
        seen_album_ids: set[str] = set()

        if include_featuring:
            groups = set(include_groups.split(","))
            groups.update(["appears_on", "compilation"])
            include_groups = ",".join(groups)

        # (album_id, album_group, is_featuring)
        albums_to_fetch: list[tuple[str, str, bool]] = []

        for item in self._paginate(
            f"{_API_BASE}/artists/{artist_id}/albums"
            f"?include_groups={include_groups}&limit=50", delay=0.0
        ):
            aid         = item.get("id")
            album_group = item.get("album_group", "album")
            if not aid or aid in seen_album_ids:
                continue
            seen_album_ids.add(aid)
            albums_to_fetch.append((aid, album_group, album_group in _FEATURING_GROUPS))

        # Fetch parallelo (max 5 richieste contemporanee)
        results: dict[str, tuple[list[TrackMetadata], str, bool]] = {}
        with ThreadPoolExecutor(max_workers=5) as executor:
            future_map = {
                executor.submit(self.get_album_tracks, aid): (aid, agroup, is_feat)
                for aid, agroup, is_feat in albums_to_fetch
            }
            for future in as_completed(future_map):
                aid, agroup, is_feat = future_map[future]
                try:
                    _, album_tracks = future.result()
                    results[aid] = (album_tracks, agroup, is_feat)
                except Exception as exc:
                    logger.warning("[spotify] album %s saltato: %s", aid, exc)

        # Ricostruzione in ordine originale
        for aid, agroup, is_feat in albums_to_fetch:
            if aid not in results:
                continue
            album_tracks, album_group, _ = results[aid]
            for track in album_tracks:
                if track.isrc and track.isrc in seen_isrc:
                    logger.debug(
                        "[spotify] duplicato saltato: %s (ISRC %s)",
                        track.title, track.isrc,
                    )
                    continue
                if is_feat and not _artist_in_track(artist_name, track.artists):
                    logger.debug(
                        "[spotify] traccia saltata (artista assente): %s — %s",
                        track.title, track.artists,
                    )
                    continue
                # Propaga album_type (assente nel vecchio codice)
                if not getattr(track, "album_type", ""):
                    track = self._replace(track, album_type=album_group)
                if track.isrc:
                    seen_isrc.add(track.isrc)
                tracks.append(track)

        return artist, tracks

    def get_url(
            self,
            spotify_url: str,
            include_featuring: bool = False,
    ) -> tuple[str, list[TrackMetadata]]:
        """
        Entry point universale: accetta qualsiasi URL/URI Spotify.

        Ora solleva InvalidUrlError (tramite parse_spotify_url) invece
        di restituire silenziosamente ("Unknown", []).
        """
        info = parse_spotify_url(spotify_url)
        t    = info["type"]

        if t == "track":
            meta = self.get_track(info["id"])
            return meta.title, [meta]

        if t == "album":
            album, tracks = self.get_album_tracks(info["id"])
            return album.get("name", "Unknown Album"), tracks

        if t == "playlist":
            pl, tracks = self.get_playlist_tracks(info["id"])
            return pl.get("name", "Unknown Playlist"), tracks

        if t in ("artist", "artist_discography"):
            artist, tracks = self.get_artist_albums(
                info["id"], include_featuring=include_featuring,
            )
            return artist.get("name", "Unknown Artist"), tracks

        raise SpotiflacError(ErrorKind.INVALID_URL, f"Tipo Spotify non supportato: {t}")

    # ------------------------------------------------------------------ helpers privati

    def _fetch_composer(self, track_id: str) -> str:
        """
        Scraping dei crediti compositore dalla embed page.

        L'API REST ufficiale non espone i crediti; questo metodo usa
        il JSON-LD nell'HTML dell'embed, simile a fetchTrackComposerWithClient
        nel codice Go (che usa invece il GraphQL interno).
        """
        url = f"https://open.spotify.com/embed/track/{track_id}"
        try:
            resp = self._session.get(url, timeout=15)
            if resp.status_code != 200:
                return ""
            match = _COMPOSER_RE.search(resp.text)
            if not match:
                return ""
            names = _NAME_RE.findall(match.group(0))
            return ", ".join(names)
        except Exception as exc:
            logger.debug("[spotify] composer scrape fallito per %s: %s", track_id, exc)
            return ""

    @staticmethod
    def _format_artists(artists: list[dict] | str) -> str:
        if isinstance(artists, str):
            return artists
        return ", ".join(
            str(a.get("name") or "Unknown") if isinstance(a, dict) else str(a)
            for a in artists
        )

    @staticmethod
    def _build_artists_data(artists: list[dict]) -> list[ArtistSimple]:
        """
        Costruisce la lista ArtistSimple {id, name, external_url}.
        Specchiato da Go ArtistSimple / formatTrackData.
        """
        result: list[ArtistSimple] = []
        for a in artists:
            if not isinstance(a, dict):
                continue
            aid  = a.get("id", "")
            name = a.get("name", "")
            ext  = (a.get("external_urls") or {}).get("spotify", "")
            if not ext and aid:
                ext = f"https://open.spotify.com/artist/{aid}"
            result.append(ArtistSimple(id=aid, name=name, external_url=ext))
        return result

    @staticmethod
    def _best_image(images: list[dict]) -> str:
        return images[0].get("url", "") if images else ""

    @staticmethod
    def _replace(track: TrackMetadata, **kwargs) -> TrackMetadata:
        """Restituisce una nuova TrackMetadata con i campi aggiornati."""
        return track.__class__(**{**track.__dict__, **kwargs})

    def _track_from_raw(self, data: dict) -> TrackMetadata:
        """
        Costruisce TrackMetadata da una risposta GET /tracks/{id}.

        Rispetto alla versione precedente aggiunge:
          preview_url, album_id, album_url, artist_id, artist_url, artists_data
        """
        album         = data.get("album", {})
        raw_artists   = data.get("artists", [])
        artists_str   = self._format_artists(raw_artists)
        artists_data  = self._build_artists_data(raw_artists)
        album_artists = self._format_artists(album.get("artists", []) or raw_artists)
        cover         = self._best_image(album.get("images") or data.get("images", []))

        copyrights     = album.get("copyrights", [])
        copyright_text = copyrights[0].get("text", "") if copyrights else ""

        first_artist = artists_data[0] if artists_data else ArtistSimple("", "", "")
        album_id     = album.get("id", "")
        album_url    = (album.get("external_urls") or {}).get("spotify", "")
        if not album_url and album_id:
            album_url = f"https://open.spotify.com/album/{album_id}"

        return TrackMetadata(
            id           = data.get("id", ""),
            title        = data.get("name", "Unknown"),
            artists      = artists_str,
            album        = album.get("name", data.get("album_name", "Unknown")),
            album_artist = album_artists,
            isrc         = data.get("external_ids", {}).get("isrc", ""),
            track_number = data.get("track_number", 0),
            disc_number  = data.get("disc_number", 1),
            total_tracks = album.get("total_tracks", 0),
            duration_ms  = data.get("duration_ms", 0),
            release_date = album.get("release_date", "") or "",
            cover_url    = cover,
            external_url = (data.get("external_urls") or {}).get("spotify", ""),
            copyright    = copyright_text,
            composer     = "",
            # --- campi nuovi ---
            preview_url  = data.get("preview_url") or "",
            album_id     = album_id,
            album_url    = album_url,
            artist_id    = first_artist.id,
            artist_url   = first_artist.external_url,
            artists_data = artists_data,
        )

    def _track_from_album_item(
            self,
            item:        dict,
            album:       dict,
            isrc:        str,
            *,
            upc:         str = "",
            publisher:   str = "",
            total_discs: int = 1,
            album_type:  str = "",
    ) -> TrackMetadata:
        """
        Costruisce TrackMetadata da un item di GET /albums/{id}/tracks.

        Rispetto alla versione precedente aggiunge:
          upc, publisher, total_discs, album_type,
          preview_url, album_id, album_url, artist_id, artist_url, artists_data
        """
        raw_artists   = item.get("artists", [])
        artists_str   = self._format_artists(raw_artists)
        artists_data  = self._build_artists_data(raw_artists)
        album_artists = self._format_artists(album.get("artists", []))
        cover         = self._best_image(album.get("images", []))

        copyrights     = album.get("copyrights", [])
        copyright_text = copyrights[0].get("text", "") if copyrights else ""

        first_artist = artists_data[0] if artists_data else ArtistSimple("", "", "")
        album_id     = album.get("id", "")
        album_url    = (album.get("external_urls") or {}).get("spotify", "")
        if not album_url and album_id:
            album_url = f"https://open.spotify.com/album/{album_id}"

        return TrackMetadata(
            id           = item.get("id", ""),
            title        = item.get("name", "Unknown"),
            artists      = artists_str,
            album        = album.get("name", "Unknown"),
            album_artist = album_artists,
            isrc         = isrc,
            track_number = item.get("track_number", 0),
            disc_number  = item.get("disc_number", 1),
            total_tracks = album.get("total_tracks", 0),
            duration_ms  = item.get("duration_ms", 0),
            release_date = album.get("release_date", "") or "",
            cover_url    = cover,
            external_url = (item.get("external_urls") or {}).get("spotify", ""),
            copyright    = copyright_text,
            composer     = "",
            # --- campi nuovi ---
            upc          = upc,
            publisher    = publisher,
            total_discs  = total_discs,
            album_type   = album_type,
            preview_url  = item.get("preview_url") or "",
            album_id     = album_id,
            album_url    = album_url,
            artist_id    = first_artist.id,
            artist_url   = first_artist.external_url,
            artists_data = artists_data,
        )