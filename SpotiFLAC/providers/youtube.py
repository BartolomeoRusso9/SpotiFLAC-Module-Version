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
from ..core.tagger import embed_metadata, EmbedOptions
from ..core.musicbrainz import mb_result_to_tags

logger = logging.getLogger(__name__)

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Parametri InnerTube allineati al JS
YT_SEARCH_PARAMS_TRACKS = "EgWKAQIIAQ=="
INNERTUBE_CLIENT_VERSION = "1.20240801.01.00"
COBALT_API_URL = "https://api.zarz.moe/v1/dl/cobalt"
_INNERTUBE_CLIENTS = [
    # ANDROID_VR: NON richiede GVS PO Token — va provato PRIMO
    {
        "name": "android_vr",
        "clientName": "ANDROID_VR",
        "clientVersion": "1.65.10",
        "ua": "com.google.android.apps.youtube.vr.oculus/1.65.10 (Linux; U; Android 12L; eureka-user Build/SQ3A.220605.009.A1) gzip",
        "extra": {
            "androidSdkVersion": 32,
            "osName": "Android", "osVersion": "12L",
            "platform": "MOBILE",
            "deviceMake": "Oculus", "deviceModel": "Quest 3",
            "hl": "en", "gl": "US",
            "timeZone": "UTC", "utcOffsetMinutes": 0,
        },
        "key": "AIzaSyA8eiZmM1FaDVjRy-df2KTyQ_vz_yYM39w",
        "clientHeader": "28",
    },
    # ANDROID: richiede PO token ma proviamo come fallback
    {
        "name": "android",
        "clientName": "ANDROID",
        "clientVersion": "21.02.35",
        "ua": "com.google.android.youtube/21.02.35 (Linux; U; Android 11) gzip",
        "extra": {
            "androidSdkVersion": 30,
            "osName": "Android", "osVersion": "11",
            "platform": "MOBILE",
            "hl": "en", "gl": "US",
            "timeZone": "UTC", "utcOffsetMinutes": 0,
        },
        "key": "AIzaSyA8eiZmM1FaDVjRy-df2KTyQ_vz_yYM39w",
        "clientHeader": "3",
    },
]

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

    def get_url(self, url: str, include_featuring: bool = False) -> Tuple[str, List[TrackMetadata]]:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)

        playlist_id = qs.get("list", [None])[0]
        if not playlist_id and "/playlist" in parsed.path:
            playlist_id = qs.get("list", [None])[0]

        if "/browse/" in parsed.path:
            browse_id = parsed.path.split("/browse/")[1].split("?")[0]
            return self._fetch_container(browse_id)

        if playlist_id:
            browse_id = playlist_id if playlist_id.startswith("VL") or playlist_id.startswith("PL") else f"VL{playlist_id}"
            return self._fetch_container(browse_id)

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
        # [Codice invariato: la logica di fetching container e pagination
        # era già sufficientemente allineata all'approccio InnerTube]
        logger.info("[youtube] Fetching container: %s", browse_id)

        url = "https://music.youtube.com/youtubei/v1/browse?alt=json"
        payload = {
            "context": {"client": {"clientName": "WEB_REMIX", "clientVersion": INNERTUBE_CLIENT_VERSION}},
            "browseId": browse_id
        }

        resp = self._session.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        title = "Unknown YouTube Container"
        try:
            header = data.get("header", {}).get("musicDetailHeaderRenderer", {})
            title = header.get("title", {}).get("runs", [{}])[0].get("text", title)
        except: pass

        tracks = []
        self._parse_tracks_from_data(data, tracks)

        continuation = self._get_continuation_token(data)
        while continuation:
            logger.debug("[youtube] Fetching continuation...")
            cont_data = self._fetch_continuation(continuation)
            if not cont_data: break

            added = self._parse_tracks_from_data(cont_data, tracks)
            if added == 0: break
            continuation = self._get_continuation_token(cont_data)

        for i, track in enumerate(tracks):
            track.track_number = i + 1

        return title, tracks

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
    # URL resolution (Allineato a Odesli JSON API e Deezer API)
    # ------------------------------------------------------------------

    def _get_youtube_url(self, metadata: TrackMetadata) -> str:
        # Se abbiamo l'URL esterno (es. Apple Music o Spotify originario), usiamo quello per Odesli!
        if metadata.external_url:
            platform_url = metadata.external_url
        elif metadata.id.startswith("tidal_"):
            platform_url = f"https://tidal.com/browse/track/{metadata.id.removeprefix('tidal_')}"
        elif metadata.id.startswith("spotify:"):
            platform_url = f"https://open.spotify.com/track/{metadata.id.split(':')[-1]}"
        else:
            # Fallback generico
            platform_url = f"https://song.link/s/{metadata.id}"

        api_url = f"https://api.song.link/v1-alpha.1/links?url={quote(platform_url)}"

        try:
            resp = self._session.get(api_url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                links = data.get("linksByPlatform", {})
                yt_info = links.get("youtubeMusic") or links.get("youtube")

                if yt_info and yt_info.get("url"):
                    yt_url = yt_info["url"]
                    logger.info("[youtube] Resolved via Odesli JSON API: %s", yt_url)
                    return yt_url
        except Exception as exc:
            logger.warning("[youtube] Odesli API failed: %s", exc)

        # Fallback alla ricerca testuale diretta se Odesli fallisce
        if metadata.title and metadata.artists:
            yt_url = self._search_youtube_direct(metadata.title, metadata.artists)
            if yt_url:
                return yt_url

        raise RuntimeError("Failed to resolve YouTube URL via Odesli API and direct search")

    def _search_youtube_direct(self, track_name: str, artist_name: str) -> str | None:
        query = f"{track_name} {artist_name}"
        url = "https://music.youtube.com/youtubei/v1/search?alt=json"

        payload = {
            "context": {"client": {"clientName": "WEB_REMIX", "clientVersion": INNERTUBE_CLIENT_VERSION}},
            "query": query,
            "params": YT_SEARCH_PARAMS_TRACKS
        }

        try:
            resp = self._session.post(url, json=payload, headers={"User-Agent": _DEFAULT_UA}, timeout=10)
            resp.raise_for_status()

            # Invece di usare una Regex fragile, sfruttiamo la funzione ricorsiva per trovare i videoId
            data = resp.json()
            video_ids = self._find_key_recursive(data, "videoId")

            if video_ids:
                video_url = f"https://music.youtube.com/watch?v={video_ids[0]}"
                logger.info("[youtube] Direct search resolved: %s", video_url)
                return video_url
        except Exception as exc:
            logger.warning("[youtube] Direct search failed: %s", exc)

        return None

    @staticmethod
    def _extract_video_id(url: str) -> str | None:
        match = re.search(r'(?:v=|/v/|youtu\.be/|/embed/)([a-zA-Z0-9_-]{11})', url)
        return match.group(1) if match else None

    # ------------------------------------------------------------------
    # Download URL APIs (Tiered Fallback Allineato al JS)
    # ------------------------------------------------------------------

    def _request_direct_innertube(self, video_id: str) -> str | None:
        """
        Prova i client InnerTube in ordine: ANDROID_VR prima (no PO token),
        poi ANDROID come fallback. Allineato all'index.js del provider mobile.
        """
        player_url = "https://www.youtube.com/youtubei/v1/player?prettyPrint=false"

        for client in _INNERTUBE_CLIENTS:
            payload = {
                "videoId": video_id,
                "contentCheckOk": True,
                "racyCheckOk": True,
                "context": {
                    "client": {
                        "clientName": client["clientName"],
                        "clientVersion": client["clientVersion"],
                        **client["extra"],
                    }
                },
            }
            headers = {
                "Content-Type": "application/json",
                "User-Agent": client["ua"],
                "Origin": "https://www.youtube.com",
                "X-YouTube-Client-Name": client["clientHeader"],
                "X-YouTube-Client-Version": client["clientVersion"],
            }
            try:
                resp = self._session.post(
                    player_url,
                    json=payload,
                    headers=headers,
                    timeout=10,
                )
                if resp.status_code != 200:
                    logger.debug("[youtube] %s: HTTP %s", client["name"], resp.status_code)
                    continue

                data = resp.json()
                if data.get("playabilityStatus", {}).get("status") != "OK":
                    logger.debug("[youtube] %s: not OK — %s", client["name"],
                                 data.get("playabilityStatus", {}).get("reason", "?"))
                    continue

                streaming = data.get("streamingData", {})
                all_formats = streaming.get("formats", []) + streaming.get("adaptiveFormats", [])

                # Preferenza itag: 251 (opus), 140 (m4a), 250, 249
                for preferred in [251, 140, 250, 249]:
                    for fmt in all_formats:
                        if fmt.get("itag") == preferred and fmt.get("url"):
                            logger.info("[youtube] %s: direct URL found (itag=%s)",
                                        client["name"], preferred)
                            return fmt["url"]

            except Exception as exc:
                logger.debug("[youtube] %s InnerTube failed: %s", client["name"], exc)

        return None

    def _request_cobalt(self, video_url: str) -> str | None:
        """
        Allineato all'index.js: usa User-Agent SpotiFLAC-Mobile senza
        Origin/Referer (così come fa il provider mobile nativo).
        """
        payload = {
            "url": video_url,
            "downloadMode": "audio",
            "audioFormat": "best",
            }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "SpotiFLAC-Mobile",  # getAppUserAgent() nel JS
        }
        try:
            resp = self._session.post(
                COBALT_API_URL,
                json=payload,
                headers=headers,
                timeout=15,
            )

            if resp.status_code in (200, 202):
                data = resp.json()
                dl_url = data.get("url") or data.get("audio") or data.get("audioUrl")
                if dl_url:
                    return dl_url
            logger.warning("[youtube] Cobalt returned HTTP %s", resp.status_code)
        except Exception as exc:
            logger.debug("[youtube] Cobalt failed: %s", exc)
        return None

    def _request_yt1d(self, video_url: str) -> str | None:
        """
        Allineato: Usa GET su /results per il nonce, POST su ajax per il dl.
        """
        try:
            res_config = self._session.get("https://yt1d.io/results/", headers={"User-Agent": _DEFAULT_UA}, timeout=10)
            nonce_match = re.search(r'"nonce"\s*:\s*"([^"]+)"', res_config.text)
            if not nonce_match: return None
            nonce = nonce_match.group(1)

            payload = {
                "action": "process_youtube_audio_download",
                "video_url": video_url,
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
                # Gestione percorsi nidificati come in JS extractYt1dDownloadURL()
                dl_url = data.get("downloadUrl") or data.get("url")
                if not dl_url and data.get("data"):
                    dl_url = data["data"].get("downloadUrl") or data["data"].get("url")

                if dl_url:
                    logger.info("[youtube] YT1D URL generated successfully")
                    return dl_url
        except Exception as exc:
            logger.warning(f"[youtube] yt1d fallback failed: {exc}")
        return None

    # ------------------------------------------------------------------
    # Metadata embedding per MP3 (ID3)
    # ------------------------------------------------------------------
    # [Codice embed_metadata invariato - la libreria Python usa standard nativi
    # diversi dalla UI JavaScript]

    def _embed_metadata(self, filepath: str, title: str, artist: str, album: str, album_artist: str, date: str, track_num: int, total_tracks: int, disc_num: int, total_discs: int, cover_url: str = "", publisher: str = "", url: str = "", lyrics: str = "", genre: str = "", bpm: str = "") -> None:
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

            if cover_url:
                try:
                    r = self._session.get(cover_url, timeout=10)
                    if r.status_code == 200:
                        audio.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=r.content))
                except Exception as exc:
                    logger.warning(f"[youtube] Cover download failed: {exc}")

            audio.save(filepath, v2_version=3)
        except Exception as exc:
            logger.warning(f"[youtube] embed_metadata failed: {exc}")

    # ------------------------------------------------------------------
    # BaseProvider interface
    # ------------------------------------------------------------------

    def download_track(self, metadata: TrackMetadata, output_dir: str, *, quality: str = "320", filename_format: str = "{title} - {artist}", position: int = 1, include_track_num: bool = False, use_album_track_num: bool = False, first_artist_only: bool = False, allow_fallback: bool = True, embed_lyrics: bool = False, lyrics_providers: list[str] | None = None, lyrics_spotify_token: str = "", enrich_metadata: bool = False, enrich_providers: list[str] | None = None, qobuz_token: str | None = None, is_album: bool = False, **kwargs) -> DownloadResult:
        try:
            dest = self._build_output_path(
                metadata, output_dir,
                filename_format=filename_format,
                position=position,
                include_track_num=include_track_num,
                use_album_track_num=use_album_track_num,
                first_artist_only=first_artist_only,
                extension=".mp3",
            )
            if self._file_exists(dest):
                return DownloadResult.ok(self.name, str(dest), fmt="mp3")

            is_native_yt = metadata.extra_info.get("provider") == "youtube"
            looks_like_yt_id = len(metadata.id) == 11 and not metadata.id.startswith("spotify:")

            if is_native_yt or looks_like_yt_id:
                video_id = metadata.id
                yt_url = f"https://www.youtube.com/watch?v={video_id}"
            else:
                yt_url = self._get_youtube_url(metadata)
                video_id = self._extract_video_id(yt_url)

            if not video_id:
                return DownloadResult.fail(self.name, "Could not extract video ID")

            from ..core.musicbrainz import AsyncMBFetch
            mb_fetcher = AsyncMBFetch(metadata.isrc) if metadata.isrc else None

            # Catena di Download: Proviamo i provider uno ad uno.
            # Se il download fallisce (es. 403 Forbidden su Direct), passiamo al prossimo.
            download_sources = [
                ("Direct", lambda: self._request_direct_innertube(video_id)),
                ("Cobalt", lambda: self._request_cobalt(yt_url)),
                ("YT1D", lambda: self._request_yt1d(yt_url))
            ]

            download_success = False

            for source_name, get_url_func in download_sources:
                dl_url = get_url_func()
                if not dl_url:
                    continue

                logger.info(f"[youtube] Attempting download via {source_name}...")
                try:
                    headers = {"User-Agent": _DEFAULT_UA}
                    with self._session.get(dl_url, headers=headers, stream=True, timeout=120) as r:
                        r.raise_for_status() # Se c'è un 403, salterà direttamente all'except qui sotto
                        total = int(r.headers.get("Content-Length") or 0)
                        downloaded = 0
                        with open(str(dest), "wb") as f:
                            for chunk in r.iter_content(chunk_size=8192):
                                if chunk:
                                    f.write(chunk)
                                    downloaded += len(chunk)
                                    if self._progress_cb and total:
                                        self._progress_cb(downloaded, total)

                    download_success = True
                    logger.info(f"[youtube] Download successful via {source_name}")
                    break  # Download riuscito, usciamo dal ciclo!

                except Exception as e:
                    logger.warning(f"[youtube] Download via {source_name} failed: {e}")
                    # Elimina eventuali file parziali o corrotti prima di passare al prossimo provider
                    if os.path.exists(str(dest)):
                        try:
                            os.remove(str(dest))
                        except OSError:
                            pass
                    continue

            if not download_success:
                return DownloadResult.fail(self.name, "All YouTube download sources failed (Direct, Cobalt, YT1D)")

            # Validazione del file finale
            from ..core.download_validation import validate_downloaded_track
            expected_s = metadata.duration_ms // 1000
            valid, err_msg = validate_downloaded_track(str(dest), expected_s)
            if not valid:
                return DownloadResult.fail(self.name, f"Validazione fallita: {err_msg}")

            mb_tags: dict[str, str] = {}
            if mb_fetcher:
                res = mb_fetcher.future.result()
                mb_tags = mb_result_to_tags(res)
                if res:
                    mapping = {
                        "mbid_track":       "MUSICBRAINZ_TRACKID",
                        "mbid_album":       "MUSICBRAINZ_ALBUMID",
                        "mbid_artist":      "MUSICBRAINZ_ARTISTID",
                        "mbid_relgroup":    "MUSICBRAINZ_RELEASEGROUPID",
                        "mbid_albumartist": "MUSICBRAINZ_ALBUMARTISTID",
                        "barcode":          "BARCODE",
                        "label":            "LABEL",
                        "organization":     "ORGANIZATION",
                        "country":          "RELEASECOUNTRY",
                        "script":           "SCRIPT",
                        "status":           "RELEASESTATUS",
                        "media":            "MEDIA",
                        "type":             "RELEASETYPE",
                        "artist_sort":      "ARTISTSORT",
                        "albumartist_sort": "ALBUMARTISTSORT",
                        "catalognumber":    "CATALOGNUMBER",
                    }
                    for mb_key, tag_name in mapping.items():
                        val = res.get(mb_key)
                        if val:
                            mb_tags[tag_name] = str(val)
                    if res.get("original_date"):
                        mb_tags["ORIGINALDATE"] = res["original_date"]
                        mb_tags["ORIGINALYEAR"] = res["original_date"][:4]
                    if res.get("catalognumber"):
                        mb_tags["CATALOGNUMBER"] = res["catalognumber"]

            opts = EmbedOptions(
                first_artist_only=first_artist_only,
                cover_url=metadata.cover_url,
                extra_tags=mb_tags,
                embed_lyrics=embed_lyrics,
                lyrics_providers=lyrics_providers or [],
                lyrics_spotify_token=lyrics_spotify_token,
                enrich=enrich_metadata,
                enrich_providers=enrich_providers,
                enrich_qobuz_token=qobuz_token or "",
                is_album=is_album,
            )
            embed_metadata(str(dest), metadata, opts, session=self._session)

            return DownloadResult.ok(self.name, str(dest), fmt="mp3")

        except SpotiflacError as exc:
            logger.error(f"[youtube] {exc}")
            return DownloadResult.fail(self.name, str(exc))
        except Exception as exc:
            logger.exception("[youtube] Unexpected error")
            return DownloadResult.fail(self.name, f"Unexpected: {exc}")