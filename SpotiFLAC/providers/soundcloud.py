import re
import json
import time
import logging
from typing import Dict, List, Optional, Any
from urllib.parse import quote, urlparse

from ..core.models import DownloadResult, TrackMetadata
from ..core.link_resolver import LinkResolver
from ..core.http import HttpClient
from .base import BaseProvider
import requests

logger = logging.getLogger(__name__)

class SoundCloudProvider(BaseProvider):
    def __init__(self):
        super().__init__() # Inizializza la classe base
        self.provider_id = "soundcloud"
        self.name = "SoundCloud"
        self.api_url = "https://api-v2.soundcloud.com"
        self.client_id = None
        self.client_id_expiry = 0
        self.cobalt_api = "https://api.zarz.moe/v1/dl/cobalt/" # Fallback Cobalt API
        self.session = requests.Session() # Sostituire con il client HTTP di SpotiFLAC se necessario

        # Simula un mobile user agent per Cobalt
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
        })

    def _fetch_client_id(self) -> str:
        """
        Estrae il client_id univoco dalle pagine HTML o dai bundle JS di SoundCloud.
        """
        logger.info("[SC] Fetching SoundCloud client_id...")
        res = self.session.get("https://soundcloud.com/")
        res.raise_for_status()

        # Strategia 1: Cerca direttamente nell'HTML
        match = re.search(r'client_id[:=]["\']([a-zA-Z0-9]{32})["\']', res.text)
        if match:
            logger.info("[SC] Found client_id in HTML")
            return match.group(1)

        # Strategia 2: Cerca nei bundle JavaScript
        script_urls = re.findall(r'src="(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"', res.text)
        if not script_urls:
            script_urls = re.findall(r'src="(https://[^"]*sndcdn\.com[^"]*\.js)"', res.text)

        # Controlla gli ultimi bundle (di solito il client_id è lì)
        for url in reversed(script_urls[-8:]):
            try:
                js_res = self.session.get(url)
                if js_res.status_code == 200:
                    cid_match = re.search(r'client_id[:=]["\']([a-zA-Z0-9]{32})["\']', js_res.text)
                    if not cid_match:
                        cid_match = re.search(r'\("client_id=([a-zA-Z0-9]{32})"\)', js_res.text)

                    if cid_match:
                        logger.info("[SC] Found client_id in JS bundle")
                        return cid_match.group(1)
            except Exception as e:
                logger.debug(f"[SC] Bundle fetch failed: {e}")

        raise ValueError("Could not find SoundCloud client_id")

    def _ensure_client_id(self):
        """Verifica se il client_id è valido, altrimenti lo rinfresca (scadenza 24h)."""
        if not self.client_id or time.time() >= self.client_id_expiry:
            self.client_id = self._fetch_client_id()
            self.client_id_expiry = time.time() + (24 * 60 * 60)

    def _api_get(self, endpoint: str, params: Dict = None) -> Any:
        """Wrapper per le chiamate API interne di SoundCloud."""
        self._ensure_client_id()
        if params is None:
            params = {}

        params['client_id'] = self.client_id
        url = f"{self.api_url}/{endpoint}"

        res = self.session.get(url, params=params)

        # Gestione token scaduto
        if res.status_code == 401:
            logger.info("[SC] Got 401, refreshing client_id...")
            self.client_id = None
            self._ensure_client_id()
            params['client_id'] = self.client_id
            res = self.session.get(url, params=params)

        res.raise_for_status()
        return res.json()

    # ==========================================
    # UTILS FORMATTAZIONE
    # ==========================================
    def _get_hires_artwork(self, url: str) -> str:
        if not url: return ""
        return url.replace("-large.", "-original.") or url.replace("-large.", "-t500x500.")

    def _format_track(self, data: Dict) -> Dict: # Idealmente restituisce un oggetto Track di SpotiFLAC/core/models.py
        if not data or not data.get('id'): return None

        user = data.get('user', {})
        pub = data.get('publisher_metadata', {})

        artist = pub.get('artist') or data.get('metadata_artist') or user.get('username', "")
        cover_url = self._get_hires_artwork(data.get('artwork_url')) or self._get_hires_artwork(user.get('avatar_url'))

        return {
            "id": str(data['id']),
            "name": data.get('title', ""),
            "artists": artist,
            "album_name": pub.get('album_title') or pub.get('release_title', ""),
            "duration_ms": data.get('full_duration') or data.get('duration', 0),
            "cover_url": cover_url,
            "isrc": pub.get('isrc') or data.get('isrc', ""),
            "provider_id": self.provider_id,
            "permalink_url": data.get('permalink_url', "")
        }

    # ==========================================
    # METODI CORE PROVIDER
    # ==========================================
    def get_track(self, track_id: str) -> Dict:
        logger.info(f"[SC] Fetching track: {track_id}")
        data = self._api_get(f"tracks/{track_id}")
        return self._format_track(data)

    def get_playlist_or_album(self, playlist_id: str) -> Dict:
        logger.info(f"[SC] Fetching playlist/album: {playlist_id}")
        data = self._api_get(f"playlists/{playlist_id}", {"representation": "full"})

        tracks = []
        track_items = data.get('tracks', [])
        need_full_fetch = []

        # Estrae le tracce. Se SoundCloud restituisce solo l'ID, le accoda per una batch request
        for i, t in enumerate(track_items):
            if t.get('title'):
                track = self._format_track(t)
                track['track_number'] = i + 1
                tracks.append(track)
            elif t.get('id'):
                need_full_fetch.append(str(t['id']))

        # Batch request per tracce incomplete (max 50 alla volta)
        if need_full_fetch:
            for i in range(0, len(need_full_fetch), 50):
                batch_ids = ",".join(need_full_fetch[i:i+50])
                try:
                    batch_data = self._api_get("tracks", {"ids": batch_ids})
                    for t in batch_data:
                        track = self._format_track(t)
                        tracks.append(track)
                except Exception as e:
                    logger.debug(f"[SC] Batch track fetch failed: {e}")

        is_album = data.get('is_album') or data.get('set_type') in ['album', 'ep', 'single', 'compilation']

        return {
            "id": str(data['id']),
            "name": data.get('title', ""),
            "type": "album" if is_album else "playlist",
            "tracks": tracks,
            "cover_url": self._get_hires_artwork(data.get('artwork_url'))
        }

    def search(self, query: str, search_type: str = "tracks", limit: int = 20) -> List[Dict]:
        logger.info(f"[SC] Searching {search_type} for: {query}")
        # search_type può essere: 'tracks', 'albums', 'users', 'playlists'
        data = self._api_get(f"search/{search_type}", {"q": query, "limit": limit, "access": "playable"})

        results = []
        for item in data.get('collection', []):
            if search_type == "tracks":
                formatted = self._format_track(item)
                if formatted: results.append(formatted)
            # Aggiungere formattazione per album/playlist in base ai models di SpotiFLAC
        return results

    # ==========================================
    # RISOLUZIONE DOWNLOAD (Stream & Cobalt)
    # ==========================================
    def get_download_url(self, track_id: str, track_permalink: str = None, audio_format: str = "mp3") -> Optional[str]:
        """
        Recupera l'URL per il download diretto o usa Cobalt come fallback.
        """
        track_data = self._api_get(f"tracks/{track_id}")
        transcodings = track_data.get('media', {}).get('transcodings', [])
        track_auth = track_data.get('track_authorization', "")

        # 1. Prova tramite le transcodifiche dirette di SoundCloud
        if transcodings and track_auth:
            best_transcoding = self._pick_best_transcoding(transcodings, audio_format)
            if best_transcoding:
                try:
                    stream_url = best_transcoding['url']
                    res = self.session.get(stream_url, params={
                        "client_id": self.client_id,
                        "track_authorization": track_auth
                    })
                    if res.status_code == 200:
                        url = res.json().get('url')
                        logger.info(f"[SC] Got direct stream URL")
                        return url
                except Exception as e:
                    logger.warning(f"[SC] Direct stream fetch failed: {e}")

        # 2. Fallback su Cobalt API
        url_to_fetch = track_permalink or track_data.get('permalink_url')
        if url_to_fetch:
            logger.info("[SC] Direct stream failed, trying Cobalt fallback...")
            try:
                payload = {
                    "url": url_to_fetch,
                    "audioFormat": audio_format,
                    "downloadMode": "audio",
                    "filenameStyle": "basic"
                }
                res = self.session.post(self.cobalt_api, json=payload, headers={"Accept": "application/json"})
                if res.status_code == 200:
                    data = res.json()
                    if data.get('status') in ['tunnel', 'redirect']:
                        logger.info("[SC] Got download URL from Cobalt")
                        return data.get('url')
            except Exception as e:
                logger.debug(f"[SC] Cobalt fallback failed: {e}")

        return None

    def get_metadata_from_url(self, url: str) -> TrackMetadata:
        """
        Estrae i metadati originali da un link SoundCloud.
        """
        if not self.client_id:
            self.client_id = self._fetch_client_id()

        # Risoluzione URL tramite API v2
        resolve_url = f"{self.api_url}/resolve?url={quote(url)}&client_id={self.client_id}"
        res = self.session.get(resolve_url)
        res.raise_for_status()
        data = res.json()

        # Estrazione degli artisti (SoundCloud usa 'user')
        artist_name = data.get("user", {}).get("username", "Unknown Artist")

        # Pulizia URL copertina per avere la massima qualità (500x500)
        artwork = data.get("artwork_url") or data.get("user", {}).get("avatar_url")
        if artwork:
            artwork = artwork.replace("-large", "-t500x500")

        # Creazione dell'oggetto TrackMetadata
        return TrackMetadata(
            id=str(data.get("id")),
            title=data.get("title"),
            artists=[artist_name],
            album="SoundCloud", # Impostiamo SoundCloud come Album per evitare conflitti
            duration_ms=data.get("full_duration", 0),
            image_url=artwork,
            release_date=data.get("created_at", ""),
            source_url=url,
            # Importante: impostiamo i metadati come "completi" per saltare ricerche esterne
            extra_info={"provider": "soundcloud", "exclusive": True}
        )

    def _pick_best_transcoding(self, transcodings: List[Dict], prefer_format: str) -> Optional[Dict]:
        """Seleziona la migliore qualità audio (evitando gli snippet)."""
        best = None
        best_score = -1

        for t in transcodings:
            if not t.get('url') or not t.get('format') or t.get('snipped'):
                continue

            score = 0
            mime = t['format'].get('mime_type', '').lower()
            protocol = t['format'].get('protocol', '').lower()

            if protocol == "progressive": score += 50
            elif protocol == "hls": score += 10

            if prefer_format == "mp3" and ("mpeg" in mime or "mp3" in mime): score += 30
            elif prefer_format == "opus" and "opus" in mime: score += 30
            elif prefer_format == "ogg" and "ogg" in mime: score += 20

            if t.get('quality') == "hq": score += 10
            elif t.get('quality') == "sq": score += 5

            if score > best_score:
                best_score = score
                best = t

        return best

    def download_track(self, metadata: TrackMetadata, output_dir: str, **kwargs) -> DownloadResult:
        """
        Metodo standard richiesto da DownloadWorker.
        """
        logger.info(f"[{self.name}] Resolving link for: {metadata.title}")

        # 1. Risoluzione tramite Odesli (flusso normale)
        from ..core.link_resolver import LinkResolver
        from ..core.http import HttpClient
        resolver = LinkResolver(HttpClient("odesli"))
        links = resolver.resolve_all(metadata.id)
        sc_url = links.get("soundcloud")

        if not sc_url:
            return DownloadResult.fail(self.provider_id, "Track not found on SoundCloud")

        # 2. Ottieni l'URL dello stream
        dl_url = self.get_download_url(track_id=None, track_permalink=sc_url)
        if not dl_url:
            return DownloadResult.fail(self.provider_id, "Stream unavailable via Cobalt/Direct")

        # 3. FIX: Accesso corretto alle property di TrackMetadata
        try:
            filename_template = kwargs.get('filename_format', "{title} - {artist}")
            filename = filename_template.format(
                title=metadata.title,
                artist=metadata.first_artist
            )

            # Pulisci il nome file da caratteri vietati
            filename = re.sub(r'[<>:"/\\|?*]', "_", filename)

            # Assicurati che la cartella di output esista
            import os
            os.makedirs(output_dir, exist_ok=True)

            file_path = os.path.join(output_dir, f"{filename}.mp3")

            # --- LOGICA DI DOWNLOAD EFFETTIVA ---
            logger.info(f"[SC] Inizio download: {file_path}")

            with self.session.get(dl_url, stream=True, timeout=30) as r:
                r.raise_for_status()

                # Se vuoi integrare la barra di avanzamento di SpotiFLAC:
                # total_size = int(r.headers.get('content-length', 0))

                with open(file_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk: # filter out keep-alive new chunks
                            f.write(chunk)

            logger.info(f"[SC] Download completato: {filename}")
            return DownloadResult.ok(self.provider_id, file_path, "mp3")
            # ------------------------------------

        except Exception as e:
            logger.error(f"[SC] Errore durante il download fisico: {e}")
            return DownloadResult.fail(self.provider_id, str(e))