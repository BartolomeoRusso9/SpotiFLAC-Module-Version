# youtube_provider.py
from __future__ import annotations

import logging
import os
import re
from typing import Callable
from urllib.parse import quote

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
    "Chrome/145.0.0.0 Safari/537.36"
)


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
    # URL resolution
    # ------------------------------------------------------------------

    def _get_youtube_url(self, spotify_track_id: str, track_name: str = "", artist_name: str = "") -> str:
        url = f"https://song.link/s/{spotify_track_id}"
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
        query      = quote(f"{track_name} {artist_name} audio")
        search_url = f"https://www.youtube.com/results?search_query={query}"
        try:
            resp  = self._session.get(search_url, timeout=10)
            resp.raise_for_status()
            match = re.search(r'"videoId":"([a-zA-Z0-9_-]{11})"', resp.text)
            if match:
                return f"https://music.youtube.com/watch?v={match.group(1)}"
        except Exception as exc:
            logger.warning("[youtube] Direct search failed: %s", exc)
        return None

    @staticmethod
    def _extract_video_id(url: str) -> str | None:
        match = re.search(r'(?:v=|/v/|youtu\.be/|/embed/)([a-zA-Z0-9_-]{11})', url)
        return match.group(1) if match else None

    # ------------------------------------------------------------------
    # Download URL APIs
    # ------------------------------------------------------------------

    def _request_spotube_dl(self, video_id: str) -> str | None:
        for engine in ("v1", "v3", "v2"):
            api_url = (
                f"https://spotubedl.com/api/download/{video_id}"
                f"?engine={engine}&format=mp3&quality=320"
            )
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
                "https://api.qwkuns.me",
                json={
                    "url":             f"https://music.youtube.com/watch?v={video_id}",
                    "audioFormat":     "mp3",
                    "audioBitrate":    "320",
                    "downloadMode":    "audio",
                    "filenameStyle":   "basic",
                    "disableMetadata": True,
                },
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") in ("tunnel", "redirect") and data.get("url"):
                    return data["url"]
        except Exception:
            pass
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

            audio.add(COMM(encoding=3, lang="eng", desc="",
                           text=["https://github.com/ShuShuzinhuu/SpotiFLAC-Module-Version"]))

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
            embed_lyrics:            bool            = False,
            lyrics_providers:        list[str] | None = None,
            lyrics_spotify_token:    str             = "",
            enrich_metadata:         bool            = False,
            enrich_providers:        list[str] | None = None,
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

            yt_url   = self._get_youtube_url(metadata.id, metadata.title, metadata.artists)
            video_id = self._extract_video_id(yt_url)
            if not video_id:
                return DownloadResult.fail(self.name, "Could not extract video ID")

            dl_url = self._request_spotube_dl(video_id) or self._request_cobalt(video_id)
            if not dl_url:
                return DownloadResult.fail(self.name, "All YouTube download APIs failed")

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

            # FIX #2: fetch_lyrics ritorna tuple[str, str] — unpacking corretto
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
                    # fetch_lyrics restituisce sempre una tupla (lyrics, provider)
                    if isinstance(result, tuple):
                        lyrics_text, lyrics_prov = result
                    else:
                        lyrics_text = result or ""

                    if lyrics_text:
                        prov_str = lyrics_prov if lyrics_prov else "sconosciuto"
                        print(f"  ✦ Testo: aggiunto tramite {prov_str}")
                except Exception as exc:
                    logger.warning("[youtube] lyrics fetch failed: %s", exc)

            # Metadata enrichment per MP3 (ID3 TCON/TBPM)
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