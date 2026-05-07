from __future__ import annotations

import re
import json
import time
import os
import logging
from typing import Dict, List, Optional, Any
from urllib.parse import quote, urlparse

from ..core.models import DownloadResult, TrackMetadata
from ..core.link_resolver import LinkResolver
from ..core.http import HttpClient
from .base import BaseProvider
from ..core.tagger import embed_metadata
import requests

logger = logging.getLogger(__name__)


class SoundCloudProvider(BaseProvider):
    def __init__(self):
        super().__init__()
        self.provider_id = "soundcloud"
        self.name        = "SoundCloud"
        self.api_url     = "https://api-v2.soundcloud.com"
        self.client_id   = None
        self.client_id_expiry = 0
        self.cobalt_api  = "https://api.zarz.moe/v1/dl/cobalt/"
        self.session     = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        })

    # ==========================================
    # CLIENT ID
    # ==========================================

    def _fetch_client_id(self) -> str:
        logger.info("[SC] Fetching SoundCloud client_id...")
        res = self.session.get("https://soundcloud.com/")
        res.raise_for_status()
        body = res.text

        # Controlla se la versione SC è cambiata — evita fetch inutili
        version_match = re.search(r'__sc_version="(\d{10})"', body)
        if version_match:
            new_version = version_match.group(1)
            if new_version == getattr(self, '_sc_version', '') and self.client_id:
                logger.info("[SC] SoundCloud version unchanged, reusing client_id")
                return self.client_id
            self._sc_version = new_version

        # Strategia 1: client_id diretto nell'HTML
        m = re.search(r'client_id[:=]["\']([a-zA-Z0-9]{32})["\']', body)
        if m:
            return m.group(1)

        # Strategia 2: JS bundles (ultimi 8, dall'ultimo al primo)
        script_urls = re.findall(
            r'src=["\'](https://[^"\']*sndcdn\.com[^"\']*\.js)["\']', body
        )
        for url in reversed(script_urls[-8:]):
            try:
                js = self.session.get(url, timeout=5)
                if js.status_code != 200:
                    continue
                js_body = js.text
                # Pattern 1: client_id:"XXXX" o client_id='XXXX'
                cm = re.search(r'client_id[:=]["\']([a-zA-Z0-9]{32})["\']', js_body)
                if not cm:
                    # Pattern 2: ("client_id=XXXX")
                    cm = re.search(r'\("client_id=([a-zA-Z0-9]{32})"\)', js_body)
                if not cm:
                    # Pattern 3: client_id=XXXX inline
                    idx = js_body.find("client_id=")
                    if idx != -1:
                        candidate = js_body[idx + 10: idx + 42]
                        if len(candidate) == 32 and candidate.isalnum():
                            return candidate
                if cm:
                    return cm.group(1)
            except Exception as e:
                logger.debug("[SC] Bundle fetch failed for %s: %s", url, e)

        raise ValueError("Could not find SoundCloud client_id")

    def _ensure_client_id(self):
        if not self.client_id or time.time() >= self.client_id_expiry:
            self.client_id = self._fetch_client_id()
            self.client_id_expiry = time.time() + 86400

    def _api_get(self, endpoint: str, params: Dict = None) -> Any:
        self._ensure_client_id()
        params = dict(params or {})
        params["client_id"] = self.client_id
        url = f"{self.api_url}/{endpoint}"
        res = self.session.get(url, params=params)
        if res.status_code == 401:
            logger.info("[SC] Got 401, refreshing client_id...")
            self.client_id = None
            self._ensure_client_id()
            params["client_id"] = self.client_id
            res = self.session.get(url, params=params)
        res.raise_for_status()
        return res.json()

    # ==========================================
    # FORMATTING UTILS
    # ==========================================

    def _get_hires_artwork(self, url: str) -> str:
        """Aggiorna l'URL copertina alla massima risoluzione disponibile."""
        if not url:
            return ""
        # Prova -t500x500. (affidabile), altrimenti lascia invariato
        if "-large." in url:
            return url.replace("-large.", "-t500x500.")
        return url

    def _format_track(self, data: Dict) -> Optional[Dict]:
        if not data or not data.get("id"):
            return None
        user = data.get("user", {})
        pub  = data.get("publisher_metadata", {})
        artist   = pub.get("artist") or data.get("metadata_artist") or user.get("username", "")
        cover_url = (
                self._get_hires_artwork(data.get("artwork_url"))
                or self._get_hires_artwork(user.get("avatar_url"))
        )
        return {
            "id":            str(data["id"]),
            "name":          data.get("title", ""),
            "artists":       artist,
            "album_name":    pub.get("album_title") or pub.get("release_title", ""),
            "duration_ms":   data.get("full_duration") or data.get("duration", 0),
            "cover_url":     cover_url,
            "isrc":          pub.get("isrc") or data.get("isrc", ""),
            "provider_id":   self.provider_id,
            "permalink_url": data.get("permalink_url", ""),
        }

    # ==========================================
    # URL UTILITIES
    # ==========================================

    def _clean_url(self, url: str) -> str:
        """
        Normalizza un URL SoundCloud:
        - rimuove query params (utm_source, ecc.)
        - normalizza m.soundcloud.com → soundcloud.com
        """
        url = url.strip()
        # Mobile URL
        url = re.sub(r'^https?://m\.soundcloud\.com', 'https://soundcloud.com', url)
        # Rimuove query string e fragment
        for ch in ('?', '#'):
            idx = url.find(ch)
            if idx != -1:
                url = url[:idx]
        return url.rstrip('/')

    def _resolve_short_link(self, url: str) -> str:
        """
        Segue il redirect di on.soundcloud.com e restituisce l'URL canonico.
        Prova prima il tag og:url nell'HTML, poi il canonical.
        """
        try:
            res = self.session.get(url, timeout=10, allow_redirects=True)
            # Metodo 1: URL finale dopo redirect
            final = res.url
            if 'soundcloud.com' in final and 'on.soundcloud.com' not in final:
                return self._clean_url(final)
            # Metodo 2: og:url nel body
            for pattern in (
                    r'<meta[^>]*property=["\']og:url["\'][^>]*content=["\']([^"\']+)["\']',
                    r'<link[^>]*rel=["\']canonical["\'][^>]*href=["\']([^"\']+)["\']',
            ):
                m = re.search(pattern, res.text, re.IGNORECASE)
                if m and 'soundcloud.com' in m.group(1):
                    return self._clean_url(m.group(1))
        except Exception as e:
            logger.warning("[SC] Short link resolution failed: %s", e)
        return url

    def _normalize_url(self, url: str) -> str:
        """Entry point unificato per la normalizzazione URL."""
        url = self._clean_url(url)
        if 'on.soundcloud.com' in url:
            url = self._resolve_short_link(url)
            url = self._clean_url(url)
        return url

    def _get_hires_artwork(self, url: str) -> str:
        """Prova la variante -original., fallback a -t500x500."""
        if not url:
            return ""
        if "-large." in url:
            # Prova prima la versione originale (qualità massima)
            original = url.replace("-large.", "-original.")
            try:
                r = self.session.head(original, timeout=4)
                if r.status_code == 200:
                    return original
            except Exception:
                pass
            return url.replace("-large.", "-t500x500.")
        return url

    # ==========================================
    # CORE PROVIDER METHODS
    # ==========================================

    def get_track(self, track_id: str) -> Dict:
        data = self._api_get(f"tracks/{track_id}")
        return self._format_track(data)

    def get_playlist_or_album(self, playlist_id: str) -> Dict:
        data   = self._api_get(f"playlists/{playlist_id}", {"representation": "full"})
        tracks = []
        need_full_fetch = []

        for i, t in enumerate(data.get("tracks", [])):
            if t.get("title"):
                track = self._format_track(t)
                if track:
                    track["track_number"] = i + 1
                    tracks.append(track)
            elif t.get("id"):
                need_full_fetch.append(str(t["id"]))

        for i in range(0, len(need_full_fetch), 50):
            batch_ids = ",".join(need_full_fetch[i:i + 50])
            try:
                batch_data = self._api_get("tracks", {"ids": batch_ids})
                for t in batch_data:
                    track = self._format_track(t)
                    if track:
                        tracks.append(track)
            except Exception as e:
                logger.debug("[SC] Batch track fetch failed: %s", e)

        is_album = data.get("is_album") or data.get("set_type") in (
            "album", "ep", "single", "compilation"
        )
        return {
            "id":       str(data["id"]),
            "name":     data.get("title", ""),
            "type":     "album" if is_album else "playlist",
            "tracks":   tracks,
            "cover_url": self._get_hires_artwork(data.get("artwork_url")),
        }

    def search(self, query: str, search_type: str = "tracks", limit: int = 20) -> List[Dict]:
        data    = self._api_get(
            f"search/{search_type}", {"q": query, "limit": limit, "access": "playable"}
        )
        results = []
        for item in data.get("collection", []):
            if search_type == "tracks":
                formatted = self._format_track(item)
                if formatted:
                    results.append(formatted)
        return results

    # ==========================================
    # METADATA HELPERS
    # ==========================================

    def _track_data_to_metadata(self, data: dict, external_url: str = "") -> TrackMetadata:
        """Converte un oggetto traccia raw dell'API SoundCloud in TrackMetadata."""
        user = data.get("user") or {}
        pub  = data.get("publisher_metadata") or {}

        artist_name = (
                pub.get("artist")
                or data.get("metadata_artist")
                or user.get("username", "Unknown Artist")
        )
        isrc = pub.get("isrc") or data.get("isrc", "")

        raw_artwork = data.get("artwork_url") or user.get("avatar_url", "")
        artwork     = self._get_hires_artwork(raw_artwork)

        raw_date = (
                pub.get("release_date")
                or data.get("display_date")
                or data.get("created_at", "")
        )
        release_date = raw_date.split("T")[0] if raw_date and "T" in raw_date else (raw_date or "")
        album_title  = pub.get("album_title") or pub.get("release_title") or "SoundCloud"
        permalink    = data.get("permalink_url", "") or external_url

        return TrackMetadata(
            id           = str(data.get("id")),
            title        = data.get("title", "Unknown"),
            artists      = artist_name,
            album_artist = artist_name,
            album        = album_title,
            duration_ms  = data.get("full_duration") or data.get("duration", 0),
            cover_url    = artwork,
            release_date = release_date,
            isrc         = isrc,
            external_url = permalink,
            extra_info   = {"provider": "soundcloud", "exclusive": True},
        )

    def _fetch_full_tracks(self, track_ids: List[str]) -> List[dict]:
        """Scarica i dati completi di una lista di track ID in batch da 50."""
        results = []
        for i in range(0, len(track_ids), 50):
            batch = track_ids[i:i + 50]
            try:
                data = self._api_get("tracks", {"ids": ",".join(batch)})
                if isinstance(data, list):
                    results.extend(data)
            except Exception as e:
                logger.warning("[SC] Batch fetch failed: %s", e)
        return results

    def _playlist_data_to_metadata_list(self, data: dict) -> List[TrackMetadata]:
        """
        Converte la risposta API di una playlist/set SoundCloud in lista di TrackMetadata.
        Le tracce "stub" (solo id, senza dati completi) vengono recuperate in batch.
        """
        tracks_raw     = data.get("tracks", [])
        playlist_cover = self._get_hires_artwork(data.get("artwork_url", ""))

        full:     List[dict] = []
        stub_ids: List[str]  = []

        for t in tracks_raw:
            if t.get("title"):
                full.append(t)
            elif t.get("id"):
                stub_ids.append(str(t["id"]))

        if stub_ids:
            full.extend(self._fetch_full_tracks(stub_ids))

        id_to_data = {str(t.get("id")): t for t in full}
        ordered: List[TrackMetadata] = []

        for i, t in enumerate(tracks_raw):
            tid        = str(t.get("id", ""))
            track_data = id_to_data.get(tid)
            if not track_data:
                continue
            meta = self._track_data_to_metadata(track_data)
            # Usa la copertina della playlist se la traccia non ne ha una
            if not meta.cover_url and playlist_cover:
                meta.cover_url = playlist_cover
            meta.track_number = i + 1
            ordered.append(meta)

        return ordered

    def _get_user_tracks_list(self, user_id: int) -> List[TrackMetadata]:
        """
        Recupera tutte le tracce pubbliche di un artista SoundCloud con paginazione.
        """
        tracks:    List[TrackMetadata] = []
        next_href: Optional[str] = (
            f"{self.api_url}/users/{user_id}/tracks"
            f"?limit=20&client_id={self.client_id}"
        )

        while next_href:
            try:
                res = self.session.get(next_href, timeout=15)
                res.raise_for_status()
                page = res.json()

                for item in page.get("collection", []):
                    if item.get("id") and item.get("title"):
                        tracks.append(self._track_data_to_metadata(item))

                next_href = page.get("next_href")
                # La next_href di SoundCloud include già il client_id — nessuna
                # aggiunta necessaria, ma verifichiamo per sicurezza.
                if next_href and "client_id" not in next_href:
                    next_href += f"&client_id={self.client_id}"

                if next_href:
                    time.sleep(0.3)

            except Exception as e:
                logger.warning("[SC] User tracks pagination failed: %s", e)
                break

        return tracks

    # ==========================================
    # ENTRY POINT UNIFICATO
    # ==========================================

    # Path da ignorare in un URL utente (non sono tracce)
    _SC_USER_SUBPAGES = {
        "sets", "albums", "tracks", "likes",
        "followers", "following", "reposts", "playlists", "popular-tracks",
    }

    def get_url(self, url: str) -> tuple[str, List[TrackMetadata]]:
        url = self._normalize_url(url)   # ← pulizia centralizzata
        self._ensure_client_id()

        resolve_url = (
            f"{self.api_url}/resolve"
            f"?url={quote(url)}&client_id={self.client_id}"
        )
        res = self.session.get(resolve_url, timeout=15)
        res.raise_for_status()
        data = res.json()

        kind = data.get("kind", "")

        if kind == "track":
            meta = self._track_data_to_metadata(data, external_url=url)
            return meta.title, [meta]
        elif kind == "playlist":
            name   = data.get("title", "Unknown Playlist")
            tracks = self._playlist_data_to_metadata_list(data)
            return name, tracks
        elif kind == "user":
            user_id   = data.get("id")
            user_name = data.get("username", "Unknown Artist")
            tracks    = self._get_user_tracks_list(user_id)
            return user_name, tracks
        else:
            raise ValueError(f"Tipo URL SoundCloud non supportato: {kind}")

    # get_metadata_from_url rimane per retrocompatibilità, ora delega a get_url
    def get_metadata_from_url(self, url: str) -> TrackMetadata:
        _, tracks = self.get_url(url)
        if not tracks:
            raise ValueError(f"Nessuna traccia trovata per: {url}")
        return tracks[0]

    # ==========================================
    # DOWNLOAD URL
    # ==========================================

    def get_download_url(
            self,
            track_id:     Optional[str],
            track_permalink: str = None,
            audio_format: str = "mp3",
    ) -> Optional[str]:
        # track_id può essere None quando arriva da Odesli (solo permalink disponibile)
        # In quel caso saltiamo la chiamata API e andiamo direttamente a Cobalt
        track_data: Dict = {}
        if track_id is not None:
            try:
                track_data   = self._api_get(f"tracks/{track_id}")
                transcodings = track_data.get("media", {}).get("transcodings", [])
                track_auth   = track_data.get("track_authorization", "")

                if transcodings and track_auth:
                    best = self._pick_best_transcoding(transcodings, audio_format)
                    if best:
                        try:
                            res = self.session.get(
                                best["url"],
                                params={"client_id": self.client_id, "track_authorization": track_auth},
                            )
                            if res.status_code == 200:
                                return res.json().get("url")
                        except Exception as e:
                            logger.warning("[SC] Direct stream fetch failed: %s", e)
            except Exception as e:
                logger.warning("[SC] Track API lookup failed: %s", e)

        url_to_fetch = track_permalink or track_data.get("permalink_url")
        if url_to_fetch:
            try:
                payload = {
                    "url":           url_to_fetch,
                    "audioFormat":   audio_format,
                    "downloadMode":  "audio",
                    "filenameStyle": "basic",
                }
                cobalt_headers = {
                    "Accept":     "application/json",
                    "User-Agent": (
                        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                        "Version/16.0 Mobile/15E148 Safari/604.1"
                    ),
                }
                res = self.session.post(self.cobalt_api, json=payload, headers=cobalt_headers)
                if res.status_code == 200:
                    cobalt_data = res.json()
                    if cobalt_data.get("status") in ("tunnel", "redirect"):
                        return cobalt_data.get("url")
            except Exception as e:
                logger.debug("[SC] Cobalt fallback failed: %s", e)

        return None

    def _pick_best_transcoding(
            self, transcodings: List[Dict], prefer_format: str
    ) -> Optional[Dict]:
        best       = None
        best_score = -1
        for t in transcodings:
            if not t.get("url") or not t.get("format") or t.get("snipped"):
                continue
            score    = 0
            mime     = t["format"].get("mime_type", "").lower()
            protocol = t["format"].get("protocol", "").lower()

            if protocol == "progressive":
                score += 50
            elif protocol == "hls":
                score += 10

            if prefer_format == "mp3" and ("mpeg" in mime or "mp3" in mime):
                score += 30
            elif prefer_format == "opus" and "opus" in mime:
                score += 30
            elif prefer_format == "ogg" and "ogg" in mime:
                score += 20

            if t.get("quality") == "hq":
                score += 10
            elif t.get("quality") == "sq":
                score += 5

            if score > best_score:
                best_score = score
                best       = t
        return best

    # ==========================================
    # DOWNLOAD TRACK (central pipeline)
    # ==========================================

    def download_track(
            self,
            metadata:    TrackMetadata,
            output_dir:  str,
            *,
            filename_format:          str             = "{title} - {artist}",
            position:                 int             = 1,
            include_track_num:        bool            = False,
            use_album_track_num:      bool            = False,
            first_artist_only:        bool            = False,
            allow_fallback:           bool            = True,
            quality:                  str             = "LOSSLESS",
            embed_lyrics:             bool            = False,
            lyrics_providers:         list[str] | None = None,
            lyrics_spotify_token:     str             = "",
            enrich_metadata:          bool            = False,
            enrich_providers:         list[str] | None = None,
            is_album:                 bool            = False,
            **kwargs,
    ) -> DownloadResult:
        logger.info("[SC] Resolving link for: %s", metadata.title)

        # ── 1. Risoluzione URL di download ────────────────────────────────
        is_native = (
                metadata.extra_info.get("provider") == "soundcloud"
                or metadata.extra_info.get("exclusive")
                or (metadata.external_url and "soundcloud.com" in metadata.external_url)
        )
        dl_url = None

        if is_native:
            sc_url = metadata.external_url or None
            dl_url = self.get_download_url(
                track_id        = metadata.id,
                track_permalink = sc_url,
            )
        else:
            try:
                resolver = LinkResolver(HttpClient("odesli"))
                links    = resolver.resolve_all(metadata.id)
                sc_url   = links.get("soundcloud")
                if sc_url:
                    dl_url = self.get_download_url(
                        track_id        = None,
                        track_permalink = sc_url,
                    )
            except Exception as e:
                logger.warning("[SC] Odesli resolution error: %s", e)

        if not dl_url:
            return DownloadResult.fail(self.provider_id, "Stream non disponibile")

        # ── 2. Costruzione percorso output (estensione .mp3) ──────────────
        dest = self._build_output_path(
            metadata, output_dir, filename_format,
            position, include_track_num, use_album_track_num, first_artist_only,
            extension=".mp3",
        )
        if self._file_exists(dest):
            return DownloadResult.ok(self.provider_id, str(dest), fmt="mp3")

        # ── 3. Download effettivo ─────────────────────────────────────────
        try:
            os.makedirs(output_dir, exist_ok=True)
            logger.info("[SC] Downloading: %s", dest.name)

            with self.session.get(dl_url, stream=True, timeout=60) as r:
                r.raise_for_status()
                total      = int(r.headers.get("Content-Length") or 0)
                downloaded = 0
                with open(str(dest), "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            if self._progress_cb and total:
                                self._progress_cb(downloaded, total)

        except Exception as e:
            logger.error("[SC] Download failed: %s", e)
            if dest.exists():
                dest.unlink(missing_ok=True)
            return DownloadResult.fail(self.provider_id, str(e))

        # ── 4. Pipeline centrale (enrichment + lyrics + tagging) ──────────
        try:
            qobuz_token = kwargs.get("qobuz_token", "") or os.environ.get("QOBUZ_AUTH_TOKEN", "")
            effective_providers = [
                p for p in (lyrics_providers or [])
                if p != "spotify"
            ]

            embed_metadata(
                dest, metadata,
                first_artist_only    = first_artist_only,
                cover_url            = metadata.cover_url,
                session              = self.session,
                embed_lyrics         = embed_lyrics,
                lyrics_providers     = effective_providers,
                lyrics_spotify_token = lyrics_spotify_token,
                enrich               = enrich_metadata,
                enrich_providers     = enrich_providers,
                enrich_qobuz_token   = qobuz_token,
                is_album             = is_album,
            )
        except Exception as exc:
            logger.warning("[SC] embed_metadata failed (file salvato senza tag): %s", exc)

        logger.info("[SC] Completed: %s", dest.name)
        return DownloadResult.ok(self.provider_id, str(dest), fmt="mp3")