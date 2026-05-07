# youtube_provider.py
from __future__ import annotations

import logging
import os
import re
from typing import Callable, List, Optional, Tuple, Dict, Any
from urllib.parse import quote, urlparse, parse_qs

import requests
from mutagen.id3 import (
    ID3, ID3NoHeaderError,
    TIT2, TPE1, TALB, TPE2, TDRC, TRCK, TPOS, APIC, TPUB, WXXX, COMM,
    USLT, TCON, TBPM,
)

from ..core.models import TrackMetadata, DownloadResult
from ..core.errors import SpotiflacError
from .base import BaseProvider

logger = logging.getLogger(__name__)

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Parametri InnerTube
YT_SEARCH_PARAMS_TRACKS = "EgWKAQIIAQ=="
INNERTUBE_CLIENT_VERSION = "1.20240801.01.00"

def _sanitize(value: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", value).strip()

def _safe_int(value) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0


class YouTubeProvider(BaseProvider):
    name = "youtube"

    def __init__(self, timeout_s: int = 120) -> None:
        super().__init__(timeout_s=timeout_s)
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _DEFAULT_UA})

    def set_progress_callback(self, cb: Callable[[int, int], None]) -> None:
        super().set_progress_callback(cb)

    # ------------------------------------------------------------------
    # URL Detection & Resolution (Playlist, Album, Artist, Track)
    # ------------------------------------------------------------------

    def get_url(self, url: str) -> Tuple[str, List[TrackMetadata]]:
        """
        Rileva se l'URL è un video singolo, playlist, album o artista e lo elabora.
        """
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)

        # Playlist o Album (browseId o list param)
        playlist_id = qs.get("list", [None])[0]
        if not playlist_id and "/playlist" in parsed.path:
            playlist_id = qs.get("list", [None])[0]

        if "/browse/" in parsed.path:
            browse_id = parsed.path.split("/browse/")[1].split("?")[0]
            return self._fetch_container(browse_id)

        if playlist_id:
            # Se inizia con OLAK5uy_, è un album visualizzato come playlist
            browse_id = playlist_id if playlist_id.startswith("VL") or playlist_id.startswith("PL") else f"VL{playlist_id}"
            return self._fetch_container(browse_id)

        # Artista (channel)
        if "/channel/" in parsed.path:
            channel_id = parsed.path.split("/channel/")[1].split("?")[0]
            return self._fetch_artist_discography(channel_id)

        # Video Singolo
        video_id = self._extract_video_id(url)
        if video_id:
            meta = self._get_single_track_metadata(video_id)
            return meta.title, [meta]

        raise ValueError(f"URL YouTube non supportato o non riconosciuto: {url}")

    def _get_single_track_metadata(self, video_id: str) -> TrackMetadata:
        url = "https://music.youtube.com/youtubei/v1/player?alt=json"
        payload = {
            "context": {"client": {"clientName": "WEB_REMIX", "clientVersion": INNERTUBE_CLIENT_VERSION}},
            "videoId": video_id
        }
        resp = self._session.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        details = data.get("videoDetails", {})
        title = details.get("title", "Unknown")
        artist = details.get("author", "Unknown Artist")
        duration = int(details.get("lengthSeconds", 0)) * 1000

        thumbs = details.get("thumbnail", {}).get("thumbnails", [])
        cover_url = thumbs[-1].get("url") if thumbs else ""

        return TrackMetadata(
            id=video_id,
            title=title,
            artists=artist,
            album_artist=artist,
            album="YouTube",
            duration_ms=duration,
            cover_url=cover_url,
            external_url=f"https://music.youtube.com/watch?v={video_id}",
            extra_info={"provider": "youtube"}
        )

    # ------------------------------------------------------------------
    # InnerTube API Fetchers per Container (Playlist/Album)
    # ------------------------------------------------------------------

    def _fetch_container(self, browse_id: str) -> Tuple[str, List[TrackMetadata]]:
        logger.info("[youtube] Fetching container: %s", browse_id)

        url = "https://music.youtube.com/youtubei/v1/browse?alt=json"
        payload = {
            "context": {"client": {"clientName": "WEB_REMIX", "clientVersion": INNERTUBE_CLIENT_VERSION}},
            "browseId": browse_id
        }

        resp = self._session.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        # Estrazione Titolo
        title = "Unknown YouTube Container"
        try:
            header = data.get("header", {}).get("musicDetailHeaderRenderer", {})
            title = header.get("title", {}).get("runs", [{}])[0].get("text", title)
        except: pass

        tracks = []
        self._parse_tracks_from_data(data, tracks)

        # Gestione Paginazione (Continuation Tokens)
        continuation = self._get_continuation_token(data)
        while continuation:
            logger.debug("[youtube] Fetching continuation...")
            cont_data = self._fetch_continuation(continuation)
            if not cont_data: break

            added = self._parse_tracks_from_data(cont_data, tracks)
            if added == 0: break
            continuation = self._get_continuation_token(cont_data)

        # Aggiungiamo i numeri traccia progressivi
        for i, track in enumerate(tracks):
            track.track_number = i + 1

        return title, tracks

    def _fetch_artist_discography(self, artist_id: str) -> Tuple[str, List[TrackMetadata]]:
        logger.info("[youtube] Fetching artist: %s", artist_id)
        title, tracks = self._fetch_container(artist_id)
        return f"Discografia: {title}", tracks

    def _parse_tracks_from_data(self, data: Dict, track_list: List[TrackMetadata]) -> int:
        count_before = len(track_list)
        items = self._find_key_recursive(data, "musicResponsiveListItemRenderer")

        for item in items:
            try:
                v_id = item.get("playlistItemData", {}).get("videoId")
                if not v_id: continue

                columns = item.get("flexColumns", [])
                title = columns[0].get("musicResponsiveListItemFlexColumnRenderer", {}).get("text", {}).get("runs", [{}])[0].get("text", "Unknown")

                artist = "Unknown Artist"
                if len(columns) > 1:
                    artist_runs = columns[1].get("musicResponsiveListItemFlexColumnRenderer", {}).get("text", {}).get("runs", [])
                    artist = ", ".join([r["text"] for r in artist_runs if "browseId" in r.get("navigationEndpoint", {}).get("browseEndpoint", {})])

                thumbnails = item.get("thumbnail", {}).get("musicThumbnailRenderer", {}).get("thumbnail", {}).get("thumbnails", [])
                cover = thumbnails[-1].get("url") if thumbnails else ""

                track_list.append(TrackMetadata(
                    id=v_id,
                    title=title,
                    artists=artist,
                    album_artist=artist,
                    album="YouTube Music",
                    duration_ms=0,
                    cover_url=cover,
                    external_url=f"https://music.youtube.com/watch?v={v_id}",
                    extra_info={"provider": "youtube"}
                ))
            except:
                continue

        return len(track_list) - count_before

    def _get_continuation_token(self, data: Dict) -> Optional[str]:
        tokens = self._find_key_recursive(data, "continuation")
        return tokens[0] if tokens else None

    def _fetch_continuation(self, token: str) -> Optional[Dict]:
        url = f"https://music.youtube.com/youtubei/v1/browse?alt=json&ctoken={quote(token)}&continuation={quote(token)}"
        try:
            resp = self._session.post(url, json={"context": {"client": {"clientName": "WEB_REMIX", "clientVersion": INNERTUBE_CLIENT_VERSION}}}, timeout=10)
            return resp.json() if resp.ok else None
        except: return None

    def _find_key_recursive(self, data: Any, key: str) -> List[Any]:
        results = []
        if isinstance(data, dict):
            for k, v in data.items():
                if k == key: results.append(v)
                else: results.extend(self._find_key_recursive(v, key))
        elif isinstance(data, list):
            for item in data:
                results.extend(self._find_key_recursive(item, key))
        return results

    # ------------------------------------------------------------------
    # URL resolution (Per scaricare da piattaforme terze tramite YT)
    # ------------------------------------------------------------------

    def _get_youtube_url(self, track_id: str, track_name: str = "", artist_name: str = "") -> str:
        if track_id.startswith("tidal_"):
            tidal_id = track_id.removeprefix("tidal_")
            url = f"https://song.link/t/{tidal_id}"
        else:
            url = f"https://song.link/s/{track_id}"

        try:
            resp = self._session.get(
                url,
                headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"},
                timeout=10,
            )
            resp.raise_for_status()
            match = re.search(r'https://(?:music\.)?youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})', resp.text)
            if not match:
                match = re.search(r'https://youtu\.be/([a-zA-Z0-9_-]{11})', resp.text)
            if match:
                yt_url = f"https://music.youtube.com/watch?v={match.group(1)}"
                logger.info("[youtube] Resolved via Songlink: %s", yt_url)
                return yt_url
        except Exception as exc:
            logger.warning("[youtube] Songlink failed: %s", exc)

        if track_name and artist_name:
            yt_url = self._search_youtube_direct(track_name, artist_name)
            if yt_url:
                return yt_url

        raise RuntimeError("Failed to resolve YouTube URL via Songlink and direct search")

    def _search_youtube_direct(self, track_name: str, artist_name: str) -> str | None:
        query = f"{track_name} {artist_name}"
        url = "https://music.youtube.com/youtubei/v1/search?alt=json"

        payload = {
            "context": {"client": {"clientName": "WEB_REMIX", "clientVersion": INNERTUBE_CLIENT_VERSION}},
            "query": query,
            "params": YT_SEARCH_PARAMS_TRACKS
        }

        try:
            resp = self._session.post(url, json=payload, headers={"User-Agent": "Mozilla/5.0 (compatible)"}, timeout=10)
            resp.raise_for_status()
            match = re.search(r'"videoId":"([a-zA-Z0-9_-]{11})"', resp.text)
            if match:
                video_url = f"https://music.youtube.com/watch?v={match.group(1)}"
                logger.info("[youtube] Direct search (InnerTube) resolved: %s", video_url)
                return video_url
        except Exception as exc:
            logger.warning("[youtube] Direct search (InnerTube) failed: %s", exc)

        return None

    @staticmethod
    def _extract_video_id(url: str) -> str | None:
        match = re.search(r'(?:v=|/v/|youtu\.be/|/embed/)([a-zA-Z0-9_-]{11})', url)
        return match.group(1) if match else None

    # ------------------------------------------------------------------
    # Download URL APIs (Tiered Fallback)
    # ------------------------------------------------------------------

    def _request_spotube_dl(self, video_id: str) -> str | None:
        for engine in ("v1", "v3", "v2"):
            api_url = f"https://spotubedl.com/api/download/{video_id}?engine={engine}&format=mp3&quality=320"
            try:
                resp = self._session.get(api_url, timeout=15)
                if resp.status_code == 200:
                    dl_url = resp.json().get("url")
                    if dl_url:
                        if dl_url.startswith("/"):
                            dl_url = "https://spotubedl.com" + dl_url
                        return dl_url
            except Exception:
                continue
        return None

    def _request_cobalt(self, video_id: str) -> str | None:
        try:
            resp = self._session.post(
                "https://api.zarz.moe/v1/dl/cobalt",
                json={
                    "url": f"https://music.youtube.com/watch?v={video_id}",
                    "downloadMode": "audio",
                    "audioFormat": "mp3",
                },
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": _DEFAULT_UA
                },
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                dl_url = data.get("url") or data.get("audio") or data.get("audioUrl")
                if dl_url:
                    return dl_url
        except Exception as exc:
            logger.debug("[youtube] Cobalt fallback failed: %s", exc)
        return None

    def _request_yt1d(self, video_id: str) -> str | None:
        try:
            res_config = self._session.get("https://yt1d.io/results/", headers={"User-Agent": _DEFAULT_UA}, timeout=10)
            nonce_match = re.search(r'"nonce"\s*:\s*"([^"]+)"', res_config.text)
            if not nonce_match: return None
            nonce = nonce_match.group(1)

            payload = {
                "action": "process_youtube_audio_download",
                "video_url": f"https://music.youtube.com/watch?v={video_id}",
                "quality": "m4a",
                "nonce": nonce
            }
            headers = {
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Origin": "https://yt1d.io",
                "Referer": "https://yt1d.io/results/",
                "User-Agent": _DEFAULT_UA
            }
            res_audio = self._session.post("https://yt1d.io/wp-admin/admin-ajax.php", data=payload, headers=headers, timeout=15)
            if res_audio.status_code == 200:
                data = res_audio.json()
                dl_url = data.get("downloadUrl") or (data.get("data") and data["data"].get("downloadUrl"))
                if dl_url:
                    return dl_url
        except Exception as exc:
            logger.debug("[youtube] yt1d fallback failed: %s", exc)
        return None

    # ------------------------------------------------------------------
    # Metadata embedding per MP3 (ID3)
    # ------------------------------------------------------------------

    def _embed_metadata(
            self,
            filepath:     str,
            title:        str,
            artist:       str,
            album:        str,
            album_artist: str,
            date:         str,
            track_num:    int,
            total_tracks: int,
            disc_num:     int,
            total_discs:  int,
            cover_url:    str = "",
            publisher:    str = "",
            url:          str = "",
            lyrics:       str = "",
            genre:        str = "",
            bpm:          str = "",
    ) -> None:
        try:
            try:
                audio = ID3(filepath)
                audio.delete()
            except ID3NoHeaderError:
                audio = ID3()

            if title:        audio.add(TIT2(encoding=3, text=str(title)))
            if artist:       audio.add(TPE1(encoding=3, text=str(artist)))
            if album:        audio.add(TALB(encoding=3, text=str(album)))
            if album_artist: audio.add(TPE2(encoding=3, text=str(album_artist)))
            if date:         audio.add(TDRC(encoding=3, text=str(date)))
            if genre:        audio.add(TCON(encoding=3, text=str(genre)))
            if bpm:          audio.add(TBPM(encoding=3, text=str(bpm)))

            audio.add(TRCK(encoding=3, text=f"{_safe_int(track_num)}/{_safe_int(total_tracks)}"))
            audio.add(TPOS(encoding=3, text=f"{_safe_int(disc_num)}/{_safe_int(total_discs)}"))

            if publisher: audio.add(TPUB(encoding=3, text=[str(publisher)]))
            if url:       audio.add(WXXX(encoding=3, desc="", url=str(url)))

            audio.add(COMM(encoding=3, lang="eng", desc="", text=["https://github.com/ShuShuzinhuu/SpotiFLAC-Module-Version"]))

            if lyrics:
                audio.add(USLT(encoding=3, lang="eng", desc="", text=lyrics))
                logger.debug("[youtube] lyrics embedded (%d chars)", len(lyrics))

            if cover_url:
                try:
                    r = self._session.get(cover_url, timeout=10)
                    if r.status_code == 200:
                        audio.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=r.content))
                except Exception as exc:
                    logger.warning("[youtube] Cover download failed: %s", exc)

            audio.save(filepath, v2_version=3)
        except Exception as exc:
            logger.warning("[youtube] embed_metadata failed: %s", exc)

    # ------------------------------------------------------------------
    # BaseProvider interface
    # ------------------------------------------------------------------

    def download_track(
            self,
            metadata:            TrackMetadata,
            output_dir:          str,
            *,
            filename_format:     str             = "{title} - {artist}",
            position:            int             = 1,
            include_track_num:   bool            = False,
            use_album_track_num: bool            = False,
            first_artist_only:   bool            = False,
            allow_fallback:      bool            = True,
            embed_lyrics:        bool            = False,
            lyrics_providers:    list[str] | None = None,
            lyrics_spotify_token:str             = "",
            enrich_metadata:     bool            = False,
            enrich_providers:    list[str] | None = None,
            is_album:            bool            = False,
            **kwargs,
    ) -> DownloadResult:
        try:
            dest = self._build_output_path(
                metadata, output_dir, filename_format,
                position, include_track_num, use_album_track_num,
                first_artist_only, extension=".mp3",
            )
            if self._file_exists(dest):
                return DownloadResult.ok(self.name, str(dest), fmt="mp3")

            # Se i metadata sono estratti direttamente da YT (tramite get_url),
            # usiamo il loro ID nativo, sennò proviamo a risolverlo.
            if metadata.extra_info.get("provider") == "youtube":
                video_id = metadata.id
            else:
                yt_url   = self._get_youtube_url(metadata.id, metadata.title, metadata.artists)
                video_id = self._extract_video_id(yt_url)

            if not video_id:
                return DownloadResult.fail(self.name, "Could not extract video ID")

            # Tiered fallback download
            dl_url = (
                    self._request_spotube_dl(video_id) or
                    self._request_cobalt(video_id) or
                    self._request_yt1d(video_id)
            )

            if not dl_url:
                return DownloadResult.fail(self.name, "All YouTube download APIs (Spotube, Cobalt, YT1D) failed")

            logger.info("[youtube] Downloading audio stream from %s", "resolved provider")
            with self._session.get(dl_url, stream=True, timeout=120) as r:
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

            artist       = metadata.artists.split(",")[0].strip() if first_artist_only else metadata.artists
            album_artist = metadata.album_artist.split(",")[0].strip() if first_artist_only else metadata.album_artist

            # Lyrics
            lyrics_text = ""
            lyrics_prov = ""
            if embed_lyrics and metadata.title and metadata.first_artist:
                try:
                    from ..core.lyrics import fetch_lyrics
                    result = fetch_lyrics(
                        track_name       = metadata.title,
                        artist_name      = metadata.first_artist,
                        album_name       = metadata.album,
                        duration_s       = metadata.duration_ms // 1000,
                        track_id         = metadata.id,
                        isrc             = metadata.isrc,
                        providers        = lyrics_providers,
                        spotify_token    = lyrics_spotify_token,
                    )
                    if isinstance(result, tuple):
                        lyrics_text, lyrics_prov = result
                    else:
                        lyrics_text = result or ""

                    if lyrics_text:
                        prov_str = lyrics_prov if lyrics_prov else "sconosciuto"
                        print(f"  ✦ Testo: aggiunto tramite {prov_str}")
                except Exception as exc:
                    logger.warning("[youtube] lyrics fetch failed: %s", exc)

            # Metadata enrichment
            genre_tag = ""
            bpm_tag   = ""
            if enrich_metadata and metadata.isrc:
                try:
                    from ..core.metadata_enrichment import enrich_metadata as _enrich
                    enriched  = _enrich(
                        track_name  = metadata.title,
                        artist_name = metadata.first_artist,
                        isrc        = metadata.isrc,
                        providers   = enrich_providers,
                    )
                    genre_tag = enriched.genre
                    bpm_tag   = str(enriched.bpm) if enriched.bpm else ""
                    if enriched._sources:
                        print(f"  [youtube] Arricchito: {enriched._sources}")
                except Exception as exc:
                    logger.warning("[youtube] enrich failed: %s", exc)

            self._embed_metadata(
                filepath     = str(dest),
                title        = metadata.title,
                artist       = artist,
                album        = metadata.album,
                album_artist = album_artist,
                date         = metadata.release_date,
                track_num    = _safe_int(metadata.track_number) or position,
                total_tracks = _safe_int(metadata.total_tracks),
                disc_num     = _safe_int(metadata.disc_number),
                total_discs  = _safe_int(metadata.total_discs),
                cover_url    = metadata.cover_url,
                lyrics       = lyrics_text,
                genre        = genre_tag,
                bpm          = bpm_tag,
            )

            return DownloadResult.ok(self.name, str(dest), fmt="mp3")

        except SpotiflacError as exc:
            logger.error("[youtube] %s", exc)
            return DownloadResult.fail(self.name, str(exc))
        except Exception as exc:
            logger.exception("[youtube] Unexpected error")
            return DownloadResult.fail(self.name, f"Unexpected: {exc}")