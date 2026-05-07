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

        match = re.search(r'client_id[:=]["\']([a-zA-Z0-9]{32})["\']', res.text)
        if match:
            return match.group(1)

        script_urls = re.findall(
            r'src=["\'](https://[^"\']*sndcdn\.com[^"\']*\.js)["\']', res.text
        )
        for url in reversed(script_urls[-15:]):
            try:
                js_res = self.session.get(url, timeout=5)
                if js_res.status_code == 200:
                    cid_match = re.search(
                        r'client_id[:=]["\']([a-zA-Z0-9]{32})["\']', js_res.text
                    )
                    if cid_match:
                        return cid_match.group(1)
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
    # METADATA FROM URL
    # ==========================================

    def get_metadata_from_url(self, url: str) -> TrackMetadata:
        """Estrae i metadati da un link SoundCloud e restituisce TrackMetadata."""
        # _ensure_client_id gestisce sia il caso None che l'expiry
        self._ensure_client_id()

        resolve_url = (
            f"{self.api_url}/resolve"
            f"?url={quote(url)}&client_id={self.client_id}"
        )
        res = self.session.get(resolve_url)
        res.raise_for_status()
        data = res.json()

        user = data.get("user") or {}
        # publisher_metadata può essere esplicitamente null nell'API → fallback a {}
        pub  = data.get("publisher_metadata") or {}

        artist_name = (
                pub.get("artist")
                or data.get("metadata_artist")
                or user.get("username", "Unknown Artist")
        )
        isrc = pub.get("isrc") or data.get("isrc", "")

        # Copertina: usa _get_hires_artwork per coerenza con il resto del provider
        raw_artwork = data.get("artwork_url") or user.get("avatar_url", "")
        artwork     = self._get_hires_artwork(raw_artwork)

        # display_date è la data di rilascio ufficiale; created_at è solo l'upload
        raw_date = (
                pub.get("release_date")
                or data.get("display_date")
                or data.get("created_at", "")
        )
        release_date = raw_date.split("T")[0] if raw_date and "T" in raw_date else (raw_date or "")

        album_title = pub.get("album_title") or pub.get("release_title") or "SoundCloud"

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
            external_url = url,
            extra_info   = {"provider": "soundcloud", "exclusive": True},
        )

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