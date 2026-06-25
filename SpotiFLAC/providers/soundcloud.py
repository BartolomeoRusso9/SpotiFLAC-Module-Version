from __future__ import annotations

import asyncio
import difflib
import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ..core.endpoints import get_soundcloud_cobalt
from ..core.errors import AuthError
from ..core.http import AsyncHttpClient
from ..core.link_resolver import LinkResolver
from ..core.models import DownloadResult, TrackMetadata, build_filename
from ..core.tagger import EmbedOptions, embed_metadata_async
from .base import BaseProvider

logger = logging.getLogger(__name__)


class SoundCloudProvider(BaseProvider):
    name = "soundcloud"

    # ==========================================
    # COSTANTI E REGEX PRE-COMPILATE
    # ==========================================
    BATCH_SIZE = 50
    CLIENT_ID_TTL = 86400
    MAX_DURATION_DIFF_MS = 10000

    _REGEX_SC_VERSION = re.compile(r'__sc_version="(\d{10})"')
    _REGEX_CLIENT_ID = re.compile(r'client_id[:=]["\']([a-zA-Z0-9]{32})["\']')
    _REGEX_CLIENT_ID_INLINE = re.compile(r'\("client_id=([a-zA-Z0-9]{32})"\)')
    _REGEX_JS_BUNDLE = re.compile(
        r'src=["\'](https://[^"\']*sndcdn\.com[^"\']*\.js)["\']'
    )
    _REGEX_OG_URL = re.compile(
        r'<meta[^>]*property=["\']og:url["\'][^>]*content=["\']([^"\']+)["\']',
        re.IGNORECASE,
    )
    _REGEX_CANONICAL_URL = re.compile(
        r'<link[^>]*rel=["\']canonical["\'][^>]*href=["\']([^"\']+)["\']', re.IGNORECASE
    )

    def __init__(self, timeout_s: int = 120):
        super().__init__(timeout_s=timeout_s)
        self.provider_id = "soundcloud"
        self.api_url = "https://api-v2.soundcloud.com"
        self.client_id: str | None = None
        self.client_id_expiry: float = 0
        self._sc_version = ""
        self.cobalt_api = get_soundcloud_cobalt()
        self._headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
        self._async_http._headers.update(self._headers)

    # ==========================================
    # CLIENT ID
    # ==========================================

    async def _fetch_client_id_async(self) -> str:
        logger.info("[SC] Fetching SoundCloud client_id...")
        headers = {"User-Agent": self._headers.get("User-Agent", "")}

        try:
            resp = await self._async_http.get(
                "https://soundcloud.com/",
                headers=headers,
                timeout=15.0,
            )
        except Exception as exc:
            raise ValueError(
                f"Network/HTTP error fetching soundcloud.com: {exc}"
            ) from exc

        body = resp.text

        version_match = self._REGEX_SC_VERSION.search(body)
        if version_match:
            new_version = version_match.group(1)
            if new_version == self._sc_version and self.client_id:
                logger.info("[SC] SoundCloud version unchanged, reusing client_id")
                return self.client_id
            self._sc_version = new_version

        m = self._REGEX_CLIENT_ID.search(body)
        if m:
            return m.group(1)

        script_urls = self._REGEX_JS_BUNDLE.findall(body)
        for url in reversed(script_urls[-8:]):
            try:
                js_resp = await self._async_http.get(url, headers=headers, timeout=5.0)
                js_body = js_resp.text
                cm = self._REGEX_CLIENT_ID.search(
                    js_body
                ) or self._REGEX_CLIENT_ID_INLINE.search(js_body)
                if not cm:
                    idx = js_body.find("client_id=")
                    if idx != -1:
                        candidate = js_body[idx + 10 : idx + 42]
                        if len(candidate) == 32 and candidate.isalnum():
                            return candidate
                if cm:
                    return cm.group(1)
            except Exception as e:
                logger.debug("[SC] Bundle fetch failed for %s: %s", url, e)

        raise ValueError("Could not find SoundCloud client_id")

    async def _ensure_client_id_async(self) -> None:
        loop_time = asyncio.get_event_loop().time()
        if not self.client_id or loop_time >= self.client_id_expiry:
            self.client_id = await self._fetch_client_id_async()
            self.client_id_expiry = asyncio.get_event_loop().time() + self.CLIENT_ID_TTL

    async def _api_get_async(
        self, endpoint: str, params: dict[str, Any] | None = None
    ) -> Any:
        """
        GET su un endpoint nominale (es. "tracks/123"), client_id iniettato
        automaticamente.  Gestisce il refresh del client_id su AuthError (401/403).
        """
        await self._ensure_client_id_async()
        params = {**(params or {}), "client_id": self.client_id}
        url = f"{self.api_url}/{endpoint}"
        headers = {"User-Agent": self._headers.get("User-Agent", "")}

        try:
            resp = await self._async_http.get(
                url, params=params, headers=headers, timeout=15.0
            )
        except AuthError:
            # _raise_for_status converte 401/403 → AuthError; qui lo gestiamo con refresh
            logger.info("[SC] Auth error on %s, refreshing client_id...", endpoint)
            self.client_id = None
            await self._ensure_client_id_async()
            params["client_id"] = self.client_id
            resp = await self._async_http.get(
                url, params=params, headers=headers, timeout=15.0
            )

        return resp.json()

    async def _api_get_url_async(self, full_url: str) -> Any:
        """
        GET on a full URL (e.g. pagination next_href).
        Injects client_id if missing and handles refresh on AuthError.
        """
        await self._ensure_client_id_async()

        if "client_id" not in full_url:
            sep = "&" if "?" in full_url else "?"
            full_url = f"{full_url}{sep}client_id={self.client_id}"

        headers = {"User-Agent": self._headers.get("User-Agent", "")}

        try:
            resp = await self._async_http.get(full_url, headers=headers, timeout=15.0)
        except AuthError:
            logger.info("[SC] Auth error on paginated URL, refreshing client_id...")
            self.client_id = None
            await self._ensure_client_id_async()
            full_url = re.sub(
                r"client_id=[^&]+", f"client_id={self.client_id}", full_url
            )
            resp = await self._async_http.get(full_url, headers=headers, timeout=15.0)

        return resp.json()

    # ==========================================
    # FORMAT UTILITIES (Sync, No I/O)
    # ==========================================

    def _get_hires_artwork(self, url: str | None) -> str:
        if not url:
            return ""
        return url.replace("-large", "-t500x500")

    def _format_track(self, data: dict[str, Any]) -> dict[str, Any] | None:
        if not data or not data.get("id"):
            return None
        user = data.get("user", {})
        pub = data.get("publisher_metadata", {})
        artist = (
            pub.get("artist") or data.get("metadata_artist") or user.get("username", "")
        )
        cover_url = self._get_hires_artwork(
            data.get("artwork_url")
        ) or self._get_hires_artwork(user.get("avatar_url"))
        return {
            "id": str(data["id"]),
            "name": data.get("title", ""),
            "artists": artist,
            "album_name": pub.get("album_title") or pub.get("release_title", ""),
            "duration_ms": data.get("full_duration") or data.get("duration", 0),
            "cover_url": cover_url,
            "isrc": pub.get("isrc") or data.get("isrc", ""),
            "provider_id": self.provider_id,
            "permalink_url": data.get("permalink_url", ""),
        }

    def _clean_url(self, url: str) -> str:
        url = re.sub(
            r"^https?://m\.soundcloud\.com", "https://soundcloud.com", url.strip()
        )
        parsed = urlsplit(url)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")).rstrip(
            "/"
        )

    def _pick_best_transcoding(
        self, transcodings: list[dict[str, Any]], prefer_format: str
    ) -> dict[str, Any] | None:
        best, best_score = None, -1
        for t in transcodings:
            if not t.get("url") or not t.get("format") or t.get("snipped"):
                continue
            score = 0
            mime = t["format"].get("mime_type", "").lower()
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
                best_score, best = score, t
        return best

    def _find_best_match(
        self,
        tracks: list[dict[str, Any]],
        target_title: str,
        target_artist: str,
        target_duration: int,
    ) -> dict[str, Any] | None:
        if not tracks:
            return None
        best_score, best_track = -1, None
        t_title_n = target_title.lower().strip()
        t_artist_n = target_artist.lower().strip()
        for t in tracks:
            score = 0
            t_title = t.get("name", "").lower().strip()
            t_artist = t.get("artists", "").lower().strip()
            t_dur = t.get("duration_ms", 0)
            score += difflib.SequenceMatcher(None, t_title_n, t_title).ratio() * 50
            score += difflib.SequenceMatcher(None, t_artist_n, t_artist).ratio() * 30
            if target_duration > 0 and t_dur > 0:
                diff_ms = abs(target_duration - t_dur)
                if diff_ms < self.MAX_DURATION_DIFF_MS:
                    score += (1 - (diff_ms / self.MAX_DURATION_DIFF_MS)) * 20
            if score > best_score:
                best_score, best_track = score, t
        return best_track if best_score >= 40 else None

    # ==========================================
    # URL UTILITIES
    # ==========================================

    async def _resolve_short_link_async(self, url: str) -> str:
        try:
            res = await self._async_http.get(url, timeout=10, follow_redirects=True)
            final = str(res.url)
            if "soundcloud.com" in final and "on.soundcloud.com" not in final:
                return self._clean_url(final)
            for pattern in (self._REGEX_OG_URL, self._REGEX_CANONICAL_URL):
                m = pattern.search(res.text)
                if m and "soundcloud.com" in m.group(1):
                    return self._clean_url(m.group(1))
        except Exception as e:
            logger.warning("[SC] Short link resolution failed: %s", e)
        return url

    async def _normalize_url_async(self, url: str) -> str:
        url = self._clean_url(url)
        if "on.soundcloud.com" in url:
            url = self._clean_url(await self._resolve_short_link_async(url))
        return url

    # ==========================================
    # ASYNC FILE UTILITIES
    # ==========================================

    async def _async_file_exists(self, path: Path) -> bool:
        """Non-blocking check: True if the file exists and is not empty."""
        exists = await asyncio.to_thread(path.exists)
        if not exists:
            return False
        size = await asyncio.to_thread(lambda: path.stat().st_size)
        if size > 0:
            print(f"Skip (already existing): {path.name}")
            logger.debug(
                "File already exists: %s (%.2f MB)", path.name, size / (1024 * 1024)
            )
            return True
        return False

    async def _async_build_output_path(
        self,
        metadata: TrackMetadata,
        output_dir: str,
        filename_format: str,
        position: int,
        include_track_num: bool,
        use_album_track_num: bool,
        first_artist_only: bool,
        extension: str = ".mp3",
    ) -> Path:
        """Versione async di _build_output_path — la mkdir viene eseguita in un thread."""
        filename = build_filename(
            metadata,
            fmt=filename_format,
            position=position,
            include_track_number=include_track_num,
            use_album_track_number=use_album_track_num,
            first_artist_only=first_artist_only,
            extension=extension,
        )
        path = Path(output_dir) / filename
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        return path

    # ==========================================
    # METODI CORE DEL PROVIDER
    # ==========================================

    async def get_track_async(self, track_id: str) -> dict[str, Any] | None:
        data = await self._api_get_async(f"tracks/{track_id}")
        return self._format_track(data)

    async def get_playlist_or_album_async(self, playlist_id: str) -> dict[str, Any]:
        data = await self._api_get_async(
            f"playlists/{playlist_id}", {"representation": "full"}
        )
        tracks, need_full_fetch = [], []

        for i, t in enumerate(data.get("tracks", [])):
            if t.get("title"):
                if track := self._format_track(t):
                    track["track_number"] = i + 1
                    tracks.append(track)
            elif t.get("id"):
                need_full_fetch.append(str(t["id"]))

        for i in range(0, len(need_full_fetch), self.BATCH_SIZE):
            batch_ids = ",".join(need_full_fetch[i : i + self.BATCH_SIZE])
            try:
                batch_data = await self._api_get_async("tracks", {"ids": batch_ids})
                for t in batch_data:
                    if track := self._format_track(t):
                        tracks.append(track)
            except Exception as e:
                logger.debug("[SC] Batch track fetch failed: %s", e)

        is_album = data.get("is_album") or data.get("set_type") in (
            "album",
            "ep",
            "single",
            "compilation",
        )
        return {
            "id": str(data["id"]),
            "name": data.get("title", ""),
            "type": "album" if is_album else "playlist",
            "tracks": tracks,
            "cover_url": self._get_hires_artwork(data.get("artwork_url")),
        }

    async def search_async(
        self, query: str, search_type: str = "tracks", limit: int = 20
    ) -> list[dict[str, Any]]:
        data = await self._api_get_async(
            f"search/{search_type}", {"q": query, "limit": limit, "access": "playable"}
        )
        items = (
            data.get("collection", [])
            if isinstance(data, dict)
            else (data if isinstance(data, list) else [])
        )
        results = []
        if search_type == "tracks":
            for item in items:
                if formatted := self._format_track(item):
                    results.append(formatted)
        return results

    # ==========================================
    # HELPER METADATI
    # ==========================================

    def _track_data_to_metadata(
        self, data: dict[str, Any], external_url: str = ""
    ) -> TrackMetadata:
        user = data.get("user") or {}
        pub = data.get("publisher_metadata") or {}
        artist_name = (
            pub.get("artist")
            or data.get("metadata_artist")
            or user.get("username", "Unknown Artist")
        )
        raw_artwork = data.get("artwork_url") or user.get("avatar_url", "")
        raw_date = (
            pub.get("release_date")
            or data.get("display_date")
            or data.get("created_at", "")
        )
        return TrackMetadata(
            id=str(data.get("id")),
            title=data.get("title", "Unknown"),
            artists=artist_name,
            album_artist=artist_name,
            album=pub.get("album_title") or pub.get("release_title") or "SoundCloud",
            duration_ms=data.get("full_duration") or data.get("duration", 0),
            cover_url=self._get_hires_artwork(raw_artwork),
            release_date=(
                raw_date.split("T")[0] if raw_date and "T" in raw_date else raw_date
            ),
            isrc=pub.get("isrc") or data.get("isrc", ""),
            external_url=data.get("permalink_url", "") or external_url,
            extra_info={"provider": "soundcloud", "exclusive": True},
        )

    async def _fetch_full_tracks_async(
        self, track_ids: list[str]
    ) -> list[dict[str, Any]]:
        results = []
        for i in range(0, len(track_ids), self.BATCH_SIZE):
            batch = track_ids[i : i + self.BATCH_SIZE]
            try:
                data = await self._api_get_async("tracks", {"ids": ",".join(batch)})
                if isinstance(data, list):
                    results.extend(data)
            except Exception as e:
                logger.warning("[SC] Batch fetch failed: %s", e)
        return results

    async def _playlist_data_to_metadata_list_async(
        self, data: dict[str, Any]
    ) -> list[TrackMetadata]:
        tracks_raw = data.get("tracks", [])
        playlist_cover = self._get_hires_artwork(data.get("artwork_url", ""))

        full, stub_ids = [], []
        for t in tracks_raw:
            if t.get("title"):
                full.append(t)
            elif t.get("id"):
                stub_ids.append(str(t["id"]))

        id_to_data = {str(t.get("id")): t for t in full}
        if stub_ids:
            for f_t in await self._fetch_full_tracks_async(stub_ids):
                t_id = str(f_t.get("id"))
                if t_id not in id_to_data:
                    id_to_data[t_id] = f_t

        ordered: list[TrackMetadata] = []
        for i, t in enumerate(tracks_raw):
            track_data = id_to_data.get(str(t.get("id", "")))
            if not track_data:
                continue
            meta = self._track_data_to_metadata(track_data)
            if not meta.cover_url and playlist_cover:
                meta.cover_url = playlist_cover
            meta.track_number = i + 1
            ordered.append(meta)

        return ordered

    async def _get_user_tracks_list_async(self, user_id: int) -> list[TrackMetadata]:
        """
        Pagina i brani di un utente usando _api_get_async per la prima pagina
        e _api_get_url_async per le successive (next_href), garantendo auth
        refresh e rate-limiting uniformi su tutto il ciclo.
        """
        tracks: list[TrackMetadata] = []

        # Prima pagina tramite helper standard (client_id iniettato automaticamente)
        try:
            page = await self._api_get_async(f"users/{user_id}/tracks", {"limit": 20})
            for item in page.get("collection", []):
                if item.get("id") and item.get("title"):
                    tracks.append(self._track_data_to_metadata(item))
            next_href: str | None = page.get("next_href")
        except Exception as e:
            logger.warning("[SC] User tracks first page failed: %s", e)
            return tracks

        # Pagine successive tramite URL completo
        while next_href:
            await asyncio.sleep(0.3)
            try:
                page = await self._api_get_url_async(next_href)
                for item in page.get("collection", []):
                    if item.get("id") and item.get("title"):
                        tracks.append(self._track_data_to_metadata(item))
                next_href = page.get("next_href")
            except Exception as e:
                logger.warning("[SC] User tracks pagination failed: %s", e)
                break

        return tracks

    # ==========================================
    # ENTRY POINT UNIFICATO
    # ==========================================

    async def get_url_async(self, url: str) -> tuple[str, list[TrackMetadata]]:
        url = await self._normalize_url_async(url)
        # _api_get_async inietta client_id e gestisce 401 refresh automaticamente
        data = await self._api_get_async("resolve", {"url": url})
        kind = data.get("kind", "")

        if kind == "track":
            meta = self._track_data_to_metadata(data, external_url=url)
            return meta.title, [meta]
        if kind == "playlist":
            return data.get(
                "title", "Unknown Playlist"
            ), await self._playlist_data_to_metadata_list_async(data)
        if kind == "user":
            return data.get(
                "username", "Unknown Artist"
            ), await self._get_user_tracks_list_async(data.get("id"))

        raise ValueError(f"SoundCloud URL type not supported: {kind}")

    async def get_metadata_from_url_async(self, url: str) -> TrackMetadata:
        _, tracks = await self.get_url_async(url)
        if not tracks:
            raise ValueError(f"No tracks found for: {url}")
        return tracks[0]

    # ==========================================
    # DOWNLOAD URL
    # ==========================================

    async def get_download_url_async(
        self,
        track_id: str | None,
        track_permalink: str | None = None,
        audio_format: str = "mp3",
    ) -> str | None:
        track_data: dict[str, Any] = {}

        if track_id is not None:
            try:
                track_data = await self._api_get_async(f"tracks/{track_id}") or {}
                transcodings = track_data.get("media", {}).get("transcodings", [])
                track_auth = track_data.get("track_authorization", "")

                if transcodings and track_auth:
                    if best := self._pick_best_transcoding(transcodings, audio_format):
                        try:
                            resp = await self._async_http.get(
                                best["url"],
                                params={
                                    "client_id": self.client_id,
                                    "track_authorization": track_auth,
                                },
                                headers={
                                    "User-Agent": self._headers.get("User-Agent", "")
                                },
                                timeout=15.0,
                            )
                            if resp.status_code == 200:
                                return resp.json().get("url")
                        except Exception as e:
                            logger.warning("[SC] Direct stream fetch failed: %s", e)
            except Exception as e:
                logger.warning("[SC] Track API lookup failed: %s", e)

        url_to_fetch = track_permalink or track_data.get("permalink_url")
        if url_to_fetch:
            try:
                payload = {
                    "url": url_to_fetch,
                    "audioFormat": audio_format,
                    "downloadMode": "audio",
                    "filenameStyle": "basic",
                }
                resp = await self._async_http.post(
                    self.cobalt_api,
                    json=payload,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": self._headers.get(
                            "User-Agent", "SpotiFLAC-Mobile/4.5.0"
                        ),
                    },
                    timeout=15.0,
                )
                if resp.status_code == 200:
                    cobalt_data = resp.json()
                    if cobalt_data.get("status") in ("tunnel", "redirect"):
                        return cobalt_data.get("url")
            except Exception as e:
                logger.debug("[SC] Cobalt fallback failed: %s", e)

        return None

    async def download_track_async(
        self,
        metadata: TrackMetadata,
        output_dir: str,
        *,
        filename_format: str = "{title} - {artist}",
        position: int = 1,
        include_track_num: bool = False,
        use_album_track_num: bool = False,
        first_artist_only: bool = False,
        allow_fallback: bool = True,
        quality: str = "LOSSLESS",
        embed_lyrics: bool = False,
        lyrics_providers: list[str] | None = None,
        enrich_metadata: bool = False,
        enrich_providers: list[str] | None = None,
        is_album: bool = False,
        **kwargs,
    ) -> DownloadResult:
        logger.info("[SC] Resolving link for: %s", metadata.title)

        is_native = (
            metadata.extra_info.get("provider") == "soundcloud"
            or metadata.extra_info.get("exclusive")
            or (metadata.external_url and "soundcloud.com" in metadata.external_url)
        )
        audio_format = "mp3"
        dl_url: str | None = None

        if is_native:
            dl_url = await self.get_download_url_async(
                track_id=metadata.id,
                track_permalink=metadata.external_url or None,
                audio_format=audio_format,
            )
        else:
            try:
                resolver = LinkResolver(AsyncHttpClient("odesli"))
                links = await resolver.resolve_all_async(metadata.id)
                if sc_url := links.get("soundcloud"):
                    dl_url = await self.get_download_url_async(
                        track_id=None,
                        track_permalink=sc_url,
                        audio_format=audio_format,
                    )
            except Exception as e:
                logger.warning("[SC] Odesli resolution error: %s", e)

            if not dl_url:
                search_query = f"{metadata.title} {metadata.artists}".strip()
                logger.info("[SC] Odesli failed. Native search for: '%s'", search_query)
                try:
                    search_results = await self.search_async(search_query, limit=5)
                    if best_track := self._find_best_match(
                        search_results,
                        metadata.title,
                        metadata.artists,
                        metadata.duration_ms,
                    ):
                        logger.info(
                            "[SC] Found fallback via search: %s (ID: %s)",
                            best_track.get("name"),
                            best_track.get("id"),
                        )
                        dl_url = await self.get_download_url_async(
                            track_id=best_track.get("id"),
                            track_permalink=best_track.get("permalink_url"),
                            audio_format=audio_format,
                        )
                    else:
                        logger.warning(
                            "[SC] No suitable fallback track found matching criteria."
                        )
                except Exception as e:
                    logger.warning("[SC] Fallback search failed: %s", e)

        if not dl_url:
            return DownloadResult.fail(self.name, "Stream non disponibile")

        # Costruzione del path output — mkdir asincrona
        dest = await self._async_build_output_path(
            metadata,
            output_dir,
            filename_format,
            position,
            include_track_num,
            use_album_track_num,
            first_artist_only,
            extension=".mp3",
        )

        if await self._async_file_exists(dest):
            return DownloadResult.skipped_result(self.name, str(dest), fmt="mp3")

        # non-blocking makedirs (dest.parent already created above, this covers output_dir)
        await asyncio.to_thread(os.makedirs, output_dir, exist_ok=True)

        try:
            logger.info("[SC] Downloading: %s", dest.name)
            await self._async_http.stream_to_file(dl_url, str(dest), self._progress_cb)
        except Exception as e:
            if "DownloadSuccessfullyStarted" in str(e):
                raise
            logger.error("[SC] Download failed: %s", e)
            if await asyncio.to_thread(dest.exists):
                await asyncio.to_thread(dest.unlink, missing_ok=True)
            return DownloadResult.fail(self.name, str(e))

        try:
            qobuz_token = kwargs.get("qobuz_token", "") or os.environ.get(
                "QOBUZ_AUTH_TOKEN", ""
            )
            effective_providers = [
                p for p in (lyrics_providers or []) if p != "spotify"
            ]
            opts = EmbedOptions(
                first_artist_only=first_artist_only,
                cover_url=metadata.cover_url,
                embed_lyrics=embed_lyrics,
                lyrics_providers=effective_providers,
                enrich=enrich_metadata,
                enrich_providers=enrich_providers,
                enrich_qobuz_token=qobuz_token or "",
                is_album=is_album,
            )
            await embed_metadata_async(str(dest), metadata, opts)
        except Exception as exc:
            logger.warning(
                "[SC] embed_metadata failed (file salvato senza tag): %s", exc
            )

        logger.info("[SC] Completed: %s", dest.name)
        return DownloadResult.ok(self.name, str(dest), fmt="mp3")
