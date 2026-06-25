import asyncio
import webview
import threading
import json
import os
import sys
import subprocess
import logging
import httpx
import aiofiles
import re
import importlib.metadata
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from .core.http import AsyncHttpClient

DEFAULT_DOWNLOAD_DIR = os.path.join(os.path.expanduser("~"), "Music", "SpotiFLAC")

class UILogHandler(logging.Handler):
    def __init__(self, api):
        super().__init__()
        self.api = api

    def emit(self, record):
        try:
            level = record.levelname
            msg   = self.format(record)
            ltype = "error" if level == "ERROR" else ("info" if level == "INFO" else "")
            self.api.log(msg, ltype)
        except Exception:
            pass


class SpotiFLAC_API:
    def __init__(self):
        self._window        = None
        self.download_dir   = DEFAULT_DOWNLOAD_DIR
        self.current_tracks = []
        self.current_url    = ""
        self.app_version = "unknown"
        # determine application version at construction time so JS can query it early
        try:
            v = importlib.metadata.version("SpotiFLAC")
        except Exception:
            v = "unknown"
            try:
                try:
                    import tomllib
                except Exception:
                    tomllib = None
                pyproj = Path(__file__).resolve().parents[1] / "pyproject.toml"
                if pyproj.exists():
                    if tomllib is not None:
                        with pyproj.open("rb") as f:
                            data = tomllib.load(f)
                            v = data.get("project", {}).get("version", v)
                    else:
                        text = pyproj.read_text(encoding="utf-8")
                        m = re.search(r'^version\s*=\s*"([^\"]+)"', text, re.MULTILINE)
                        if m:
                            v = m.group(1)
            except Exception:
                pass
        self.app_version = v

    def set_window(self, window):
        self._window = window

    def _on_loaded(self):
        self.log("Python Backend connected.", "info")
        self.log(f"Default download folder: {self.download_dir}", "info")
        self._check_ffmpeg_startup()
        app_version = self.app_version
        try:
            if self._window:
                self._window.evaluate_js("window.loadHistoryAndProfiles();")
                self._window.evaluate_js(f"document.getElementById('tb-version').innerText = '{app_version}';")
                self._window.evaluate_js(f"document.getElementById('hero-version').innerText = 'v{app_version}';")
        except Exception:
            pass

    # Expose simple getters to the frontend via pywebview
    def get_version(self):
        return self.app_version

    def get_latest_version(self) -> dict:
        async def _inner():
            try:
                client = AsyncHttpClient("github", timeout_s=10)
                resp = await client.get(
                    "https://api.github.com/repos/ShuShuzinhuu/SpotiFLAC-Module-Version/releases/latest",
                    headers={"Accept": "application/vnd.github.v3+json", "User-Agent": "SpotiFLAC-Desktop"},
                    timeout=10,
                )
                if resp.status_code != 200:
                    return {"latest_version": "", "published_at": ""}
                data = resp.json() or {}
                return {
                    "latest_version": str(data.get("tag_name", "") or "").lstrip("v").strip(),
                    "published_at": str(data.get("published_at", "") or ""),
                }
            except Exception:
                return {"latest_version": "", "published_at": ""}
        return asyncio.run(_inner())

    def _check_ffmpeg_startup(self) -> None:
        try:
            from .core.ffmpeg_check import check_ffmpeg
            result = check_ffmpeg()
            if result["available"]:
                short = result["version"][:80]
                self.log(f"ffmpeg: {short}", "ok")
            else:
                self.log(
                    "⚠  ffmpeg not found — Tidal FLAC muxing and Amazon "
                    "decryption will fail. Install: https://ffmpeg.org/download.html",
                    "error",
                )
                try:
                    if self._window:
                        import json
                        self._window.evaluate_js(
                            f"window.showFfmpegWarning({json.dumps(result)});"
                        )
                except Exception:
                    pass
        except Exception as exc:
            self.log(f"ffmpeg check error: {exc}", "warn")

    # ── Metodo pubblico opzionale (il JS può interrogarlo anche dopo) ─────────────
    def get_ffmpeg_status(self) -> dict:
        from .core.ffmpeg_check import check_ffmpeg
        return check_ffmpeg()

    # ── UI communication ──────────────────────────────────────────────────────

    def log(self, message, type=""):
        safe = json.dumps(str(message))
        safe_type = json.dumps(type)
        try:
            if self._window:
                self._window.evaluate_js(f"window.app_log({safe}, {safe_type});")
        except Exception:
            pass

    def set_progress(self, label=""):
        safe_label = json.dumps(label)
        try:
            if self._window:
                self._window.evaluate_js(f"window.app_set_progress({safe_label});")
        except Exception:
            pass

    def set_metadata(
        self,
        title,
        artist,
        cover="",
        quality="FLAC",
        playlist_description=None,
        playlist_followers=None,
        playlist_owner="",
        playlist_owner_avatar="",
        source="",
        artist_listeners=None,
        artist_rank=None,
        artist_verified=False,
        artist_biography=None,
        release_date=None,
        track_count=None,
    ):
        payload = {
            "title": title,
            "artist": artist,
            "cover": cover,
            "quality": quality,
        }
        if playlist_description is not None:
            payload["description"] = playlist_description
        if playlist_followers is not None:
            payload["followers"] = playlist_followers
        if playlist_owner:
            payload["owner"] = playlist_owner
        if playlist_owner_avatar:
            payload["owner_avatar"] = playlist_owner_avatar
        if source:
            payload["source"] = source
        if artist_listeners is not None:
            payload["artist_listeners"] = artist_listeners
        if artist_rank is not None:
            payload["artist_rank"] = artist_rank
        if artist_verified:
            payload["artist_verified"] = artist_verified
        if artist_biography:
            payload["artist_biography"] = artist_biography
        if release_date:
            payload["release_date"] = release_date
        if track_count is not None:
            payload["track_count"] = track_count

        data = json.dumps(payload)
        try:
            if self._window:
                self._window.evaluate_js(f"window.app_set_metadata({data});")
        except Exception:
            pass

    def _fetch_track_playcounts(self, sp_client, track_ids: list[str]) -> dict[str, dict]:
        """Retrieves playcount per track in parallel using get_track_stats."""
        stats_map: dict[str, dict] = {}
        unique_ids = [tid for tid in dict.fromkeys(track_ids) if tid]
        if not unique_ids:
            return stats_map

        max_workers = min(8, len(unique_ids))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_id = {
                executor.submit(sp_client.get_track_stats, track_id): track_id
                for track_id in unique_ids
            }
            for future in as_completed(future_to_id):
                track_id = future_to_id[future]
                try:
                    stats = future.result()
                    if stats.get('playcount'):
                        stats_map[track_id] = stats
                except Exception:
                    continue
        return stats_map

    # ── Profile & History API ─────────────────────────────────────────────────

    def save_settings(self, cfg: dict):
        try:
            settings_file = Path.home() / ".cache" / "spotiflac" / "gui-settings.json"
            settings_file.parent.mkdir(parents=True, exist_ok=True)
            settings_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        except Exception as e:
            self.log(f"Failed to save settings: {e}", "error")

    def load_settings(self) -> dict:
        try:
            settings_file = Path.home() / ".cache" / "spotiflac" / "gui-settings.json"
            if settings_file.exists():
                return json.loads(settings_file.read_text(encoding="utf-8"))
        except Exception as e:
            self.log(f"Failed to load settings: {e}", "error")
        return {}

    def get_history(self):
        try:
            from .core.session_memory import get_url_history
            return get_url_history()
        except Exception:
            return []

    def get_profiles(self):
        try:
            from .core.profiles import list_profiles
            return list_profiles()
        except Exception:
            return []

    def load_profile_data(self, name):
        try:
            from .core.profiles import get_profile
            return get_profile(name) or {}
        except Exception:
            return {}
        
    def cache_image(self, url):
        return url
    
    def get_spotify_home_feed(self):
        """Metodo chiamato da app.js per retrievesre l'Home Feed."""
        try:
            from .core.spotfetch import SpotifyWebClient
            from .providers.spotify_metadata import parse_home_feed
            
            client = SpotifyWebClient()
            raw_data = client.get_home_feed()
            formatted_data = parse_home_feed(raw_data)
            return formatted_data
        except Exception as e:
            import logging
            logging.error(f"Error retrieving Home Feed: {e}")
            return {"success": False, "error": str(e)}

    def search_provider(self, query, limit=50):
        """Search music providers (Spotify) for metadata matching `query`.

        Returns a dictionary with 4 sections: tracks, albums, artists, playlists (max 50 results each).
        """
        try:
            from .providers.spotify_metadata import SpotifyMetadataClient
            client = SpotifyMetadataClient()
            # client.search() restituisce già un dizionario con i 4 array
            results = client.search(query, limit=limit)
            
            out = {
                "tracks": [],
                "albums": [],
                "artists": [],
                "playlists": []
            }
            
            # --- Tracks ---
            for t in results.get("tracks", [])[:limit]:
                out["tracks"].append({
                    "id": getattr(t, 'id', ''),
                    "name": getattr(t, 'title', ''),      # Formato Go
                    "title": getattr(t, 'title', ''),     # Formato Legacy
                    "type": "track",
                    "artists": getattr(t, 'artists', ''), # Formato Go
                    "artist": getattr(t, 'artists', ''),  # Formato Legacy
                    "album_name": getattr(t, 'album', ''),
                    "album": getattr(t, 'album', ''),
                    "duration_ms": getattr(t, 'duration_ms', 0),
                    "images": getattr(t, 'cover_url', ''), # Formato Go
                    "cover": getattr(t, 'cover_url', ''),  # Formato Legacy
                    "external_urls": getattr(t, 'external_url', ''),
                    "external_url": getattr(t, 'external_url', ''),
                    "preview_url": getattr(t, 'preview_url', ''),
                    "playcount": getattr(t, 'plays', ''),
                    "is_explicit": getattr(t, 'is_explicit', False),
                    "explicit": getattr(t, 'is_explicit', False),
                    "isrc": getattr(t, 'isrc', ''),
                    "provider": "spotify",
                })
                
            # --- Albums ---
            for a in results.get("albums", [])[:limit]:
                out["albums"].append({
                    "id": a.get("id", ""),
                    "name": a.get("name", ""),
                    "title": a.get("name", ""),
                    "type": "album",
                    "artists": a.get("artists", ""),
                    "artist": a.get("artists", ""),
                    "images": a.get("cover_url", ""),
                    "cover": a.get("cover_url", ""),
                    "release_date": a.get("release_date", ""),
                    "external_urls": a.get("external_url", ""),
                    "external_url": a.get("external_url", ""),
                    "provider": "spotify",
                })

            # --- Artists ---
            for art in results.get("artists", [])[:limit]:
                out["artists"].append({
                    "id": art.get("id", ""),
                    "name": art.get("name", ""),
                    "title": art.get("name", ""),
                    "type": "artist",
                    "images": art.get("cover_url", ""),
                    "cover": art.get("cover_url", ""),
                    "external_urls": art.get("external_url", ""),
                    "external_url": art.get("external_url", ""),
                    "provider": "spotify",
                })

            # --- Playlists ---
            for p in results.get("playlists", [])[:limit]:
                out["playlists"].append({
                    "id": p.get("id", ""),
                    "name": p.get("name", ""),
                    "title": p.get("name", ""),
                    "type": "playlist",
                    "owner": p.get("owner", ""),
                    "images": p.get("cover_url", ""),
                    "cover": p.get("cover_url", ""),
                    "external_urls": p.get("external_url", ""),
                    "external_url": p.get("external_url", ""),
                    "provider": "spotify",
                })

            return out
        except Exception as e:
            self.log(f"search_provider error: {e}", "error")
            return {"tracks": [], "albums": [], "artists": [], "playlists": []}

    def _search_provider_thread(self, query, limit):
        try:
            from .providers.spotify_metadata import SpotifyMetadataClient
            client = SpotifyMetadataClient()
            results = client.search(query, limit=limit)
            
            out = {
                "tracks": [],
                "albums": [],
                "artists": [],
                "playlists": []
            }
            
            # --- Tracks ---
            for t in results.get("tracks", [])[:limit]:
                out["tracks"].append({
                    "id": getattr(t, 'id', ''),
                    "name": getattr(t, 'title', ''),
                    "title": getattr(t, 'title', ''),
                    "type": "track",
                    "artists": getattr(t, 'artists', ''),
                    "artist": getattr(t, 'artists', ''),
                    "album_name": getattr(t, 'album', ''),
                    "album": getattr(t, 'album', ''),
                    "duration_ms": getattr(t, 'duration_ms', 0),
                    "images": getattr(t, 'cover_url', ''),
                    "cover": getattr(t, 'cover_url', ''),
                    "external_urls": getattr(t, 'external_url', ''),
                    "external_url": getattr(t, 'external_url', ''),
                    "preview_url": getattr(t, 'preview_url', ''),
                    "playcount": getattr(t, 'plays', ''),
                    "is_explicit": getattr(t, 'is_explicit', False),
                    "explicit": getattr(t, 'is_explicit', False),
                    "isrc": getattr(t, 'isrc', ''),
                    "provider": "spotify",
                })

            # --- Albums ---
            for a in results.get("albums", [])[:limit]:
                out["albums"].append({
                    "id": a.get("id", ""),
                    "name": a.get("name", ""),
                    "title": a.get("name", ""),
                    "type": "album",
                    "artists": a.get("artists", ""),
                    "artist": a.get("artists", ""),
                    "images": a.get("cover_url", ""),
                    "cover": a.get("cover_url", ""),
                    "release_date": a.get("release_date", ""),
                    "external_urls": a.get("external_url", ""),
                    "external_url": a.get("external_url", ""),
                    "provider": "spotify",
                })

            # --- Artists ---
            for art in results.get("artists", [])[:limit]:
                out["artists"].append({
                    "id": art.get("id", ""),
                    "name": art.get("name", ""),
                    "title": art.get("name", ""),
                    "type": "artist",
                    "images": art.get("cover_url", ""),
                    "cover": art.get("cover_url", ""),
                    "external_urls": art.get("external_url", ""),
                    "external_url": art.get("external_url", ""),
                    "provider": "spotify",
                })

            # --- Playlists ---
            for p in results.get("playlists", [])[:limit]:
                out["playlists"].append({
                    "id": p.get("id", ""),
                    "name": p.get("name", ""),
                    "title": p.get("name", ""),
                    "type": "playlist",
                    "owner": p.get("owner", ""),
                    "images": p.get("cover_url", ""),
                    "cover": p.get("cover_url", ""),
                    "external_urls": p.get("external_url", ""),
                    "external_url": p.get("external_url", ""),
                    "provider": "spotify",
                })

            payload = json.dumps(out)
            if self._window:
                # Il JS ora riceverà un oggetto completo come nella versione Go
                self._window.evaluate_js(f"window.app_handle_provider_search_results({payload});")
        except Exception as e:
            msg = json.dumps(str(e))
            if self._window:
                self._window.evaluate_js(f"window.app_handle_provider_search_error({msg});")

    def search_provider_async(self, query, limit=50): # Limite di default updated a 50
        if not query:
            return {"status": "empty"}
        threading.Thread(target=self._search_provider_thread, args=(query, limit), daemon=True).start()
        return {"status": "started"}

    def search_code(self, query, path='.', limit=200):
        """Search repository codebase (substring case-insensitive).

        Returns list of {path, line, snippet}.
        """
        try:
            from .core.code_search import search_code
            results = search_code(query, path=path or '.', limit=limit or 200)
            return results
        except Exception as e:
            self.log(f"search_code error: {e}", "error")
            return []

    def remove_history_item(self, url):
        try:
            from .core.session_memory import remove_url_from_history
            remove_url_from_history(url)
        except Exception:
            pass

    def get_network_status(self):
        async def _inner():
            try:
                client = AsyncHttpClient("network", timeout_s=10)
                resp = await client.get("https://ipapi.co/json/", timeout=10)
                data = resp.json() if resp.status_code == 200 else {}
                return {
                    "ip": data.get("ip", "Unavailable"),
                    "country_name": data.get("country_name", "Unknown"),
                    "country_code": data.get("country_code", ""),
                }
            except Exception:
                return {"ip": "Unavailable", "country_name": "Unknown", "country_code": ""}
        return asyncio.run(_inner())

    def save_profile_data(self, name, cfg):
        try:
            from .core.profiles import save_profile
            save_profile(name, cfg)
            self.log(f"Profile '{name}' saved successfully.", "ok")
            return True
        except Exception as e:
            self.log(f"Failed to save profile: {e}", "error")
            return False

    def delete_profile_data(self, name):
        try:
            from .core.profiles import delete_profile
            deleted = delete_profile(name)
            self.log(f"Profile '{name}' deleted: {deleted}.", "ok" if deleted else "warn")
            return deleted
        except Exception as e:
            self.log(f"Failed to delete profile: {e}", "error")
            return False

    def check_qobuz_api(self, url):
        return self._check_api_endpoint_sync(url)

    def check_tidal_api(self, url):
        return self._check_api_endpoint_sync(url)

    def _check_api_endpoint_sync(self, url):
        async def _inner():
            try:
                if not url or not isinstance(url, str) or not url.strip():
                    raise ValueError('URL must be a non-empty string')
                normalized = url.strip()
                if not normalized.lower().startswith('http'):
                    raise ValueError('URL must start with http or https')
                client = AsyncHttpClient("api-check", timeout_s=10)
                resp = await client.get(normalized, follow_redirects=True, timeout=10.0)
                return {'ok': 200 <= resp.status_code < 400, 'status_code': resp.status_code, 'url': str(resp.url)}
            except Exception as e:
                return {'ok': False, 'error': str(e)}
        return asyncio.run(_inner())

    # ── Window controls ───────────────────────────────────────────────────────

    def WindowMinimise(self):
        if self._window:
            self._window.minimize()

    def WindowToggleMaximise(self):
        if not self._window:
            return
        if getattr(self, '_is_maximized', False):
            self._window.restore()
            self._is_maximized = False
        else:
            if sys.platform == 'win32':
                try:
                    import ctypes
                    # Get monitor work area (excluding taskbar)
                    work_area = ctypes.wintypes.RECT()
                    # SPI_GETWORKAREA = 48
                    result = ctypes.windll.user32.SystemParametersInfoW(
                        48, 0, ctypes.byref(work_area), 0
                    )
                    if result:
                        width = work_area.right - work_area.left
                        height = work_area.bottom - work_area.top
                        # Use SetWindowPos API for more reliable positioning
                        hwnd = ctypes.windll.user32.FindWindowW(None, self._window.title)
                        if hwnd:
                            # SWP_NOZORDER = 4, SWP_FRAMECHANGED = 0x20
                            ctypes.windll.user32.SetWindowPos(
                                hwnd, 
                                None,
                                work_area.left,
                                work_area.top,
                                width,
                                height,
                                4 | 0x20
                            )
                        else:
                            self._window.move(work_area.left, work_area.top)
                            self._window.resize(width, height)
                        self._is_maximized = True
                    else:
                        # Fallback - use maximize but with adjustment
                        self._window.maximize()
                        self._is_maximized = True
                except Exception as e:
                    self.log(f"Maximize error: {e}", "warn")
                    try:
                        self._window.maximize()
                        self._is_maximized = True
                    except Exception:
                        pass
            else:
                self._window.maximize()
                self._is_maximized = True

    def Quit(self):
        if self._window:
            try:
                self._window.destroy()
            except Exception:
                pass
        os._exit(0)

    def choose_folder(self):
        if self._window:
            result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
            if result and len(result) > 0:
                self.download_dir = result[0]
                self.log(f"Download folder changed: {self.download_dir}", "ok")
                try:
                    self._window.evaluate_js(f"window.updateFolderLabel({json.dumps(self.download_dir)});")
                except Exception:
                    pass

    def open_config_folder(self):
        config_dir = os.path.join(os.path.expanduser('~'), '.cache', 'spotiflac')
        try:
            os.makedirs(config_dir, exist_ok=True)
            if sys.platform == 'darwin':
                subprocess.Popen(['open', config_dir])
            elif sys.platform == 'win32':
                os.startfile(config_dir)
            else:
                subprocess.Popen(['xdg-open', config_dir])
            self.log(f"Opened config folder: {config_dir}", "ok")
        except Exception as e:
            self.log(f"Failed opening config folder: {e}", "error")

    def open_url(self, url):
        import webbrowser
        webbrowser.open(url)

    # ── Lyrics download (separate .lrc file) ──────────────────────────────────

    def download_track_lyrics(self, track_data):
        """Download and save lyrics as a separate .lrc file for a single track."""
        threading.Thread(
            target=self._download_lyrics_task,
            args=(track_data,),
            daemon=True,
        ).start()

    def _download_lyrics_task(self, track_data):
        asyncio.run(self._download_lyrics_task_async(track_data))

    async def _download_lyrics_task_async(self, track_data):
        try:
            title   = track_data.get("title", "Unknown")
            artist  = track_data.get("artist", "")
            isrc    = track_data.get("isrc", "")
            dur_ms  = track_data.get("duration_ms", 0)
            track_id = track_data.get("id", "")

            from .core.lyrics import fetch_lyrics_async
            lyrics_text, provider = await fetch_lyrics_async(
                track_name  = title,
                artist_name = artist,
                duration_s  = dur_ms // 1000 if dur_ms else 0,
                track_id    = track_id,
                isrc        = isrc,
            )

            if not lyrics_text:
                self.log(f"No lyrics found for: {title}", "error")
                return

            safe_title  = re.sub(r'[\\/*?:"<>|]', "", title).strip()
            safe_artist = re.sub(r'[\\/*?:"<>|]', "", artist).strip()
            filename    = f"{safe_artist} - {safe_title}.lrc" if safe_artist else f"{safe_title}.lrc"
            out_path    = os.path.join(self.download_dir, filename)

            os.makedirs(self.download_dir, exist_ok=True)
            async with aiofiles.open(out_path, "w", encoding="utf-8") as f:
                await f.write(lyrics_text)

            self.log(f"Lyrics saved: {filename} (via {provider})", "ok")

        except Exception as e:
            self.log(f"Lyrics download error: {e}", "error")

    # ── Cover download (separate .jpg file) ───────────────────────────────────

    def download_track_cover(self, track_data):
        """Download and save album cover as a separate .jpg file."""
        threading.Thread(
            target=self._download_cover_task,
            args=(track_data,),
            daemon=True,
        ).start()


    def _download_cover_task(self, track_data):
        asyncio.run(self._download_cover_task_async(track_data))

    async def _download_cover_task_async(self, track_data):
        try:
            title     = track_data.get("title", "Unknown")
            artist    = track_data.get("artist", "")
            cover_url = track_data.get("cover", "")

            if not cover_url:
                self.log(f"No cover URL available for: {title}", "error")
                return

            self.log(f"Downloading cover for: {title}…", "info")
            client = AsyncHttpClient("cover", timeout_s=15)
            resp = await client.get(cover_url, timeout=15)
            resp.raise_for_status()

            safe_title  = re.sub(r'[\\/*?:"<>|]', "", title).strip()
            safe_artist = re.sub(r'[\\/*?:"<>|]', "", artist).strip()
            filename    = f"{safe_artist} - {safe_title}.jpg" if safe_artist else f"{safe_title}.jpg"
            out_path    = os.path.join(self.download_dir, filename)

            os.makedirs(self.download_dir, exist_ok=True)
            async with aiofiles.open(out_path, "wb") as f:
                await f.write(resp.content)

            self.log(f"Cover saved: {filename}", "ok")

        except Exception as e:
            self.log(f"Cover download error: {e}", "error")

    def download_cover(self, cover_data):
        """Download and save cover with appropriate folder structure based on type."""
        threading.Thread(
            target=self._download_cover_task_typed,
            args=(cover_data,),
            daemon=True,
        ).start()

    def _download_cover_task_typed(self, cover_data):
        asyncio.run(self._download_cover_task_typed_async(cover_data))

    async def _download_cover_task_typed_async(self, cover_data):
        try:
            title     = cover_data.get("title", "Unknown")
            artist    = cover_data.get("artist", "")
            owner     = cover_data.get("owner", "")
            cover_url = cover_data.get("cover", "")
            item_type = cover_data.get("type", "ALBUM").upper()

            if not cover_url:
                self.log(f"No cover URL available for: {title}", "error")
                return


            safe_title  = re.sub(r'[\\/*?:"<>|]', "", title).strip()
            safe_artist = re.sub(r'[\\/*?:"<>|]', "", artist).strip()
            re.sub(r'[\\/*?:"<>|]', "", owner).strip()
            client = AsyncHttpClient("cover", timeout_s=15)
            resp = await client.get(cover_url, timeout=15)
            resp.raise_for_status()

            # Determine folder structure based on type
            if item_type == 'PLAYLIST':
                # Playlist/cover.jpg
                folder_path = os.path.join(self.download_dir, safe_title)
                folder_display = safe_title
            elif item_type == 'ARTIST':
                # Artist/cover.jpg
                folder_path = os.path.join(self.download_dir, safe_artist)
                folder_display = safe_artist
            else:  # ALBUM or TRACK
                # Artist/Album/cover.jpg
                folder_path = os.path.join(self.download_dir, safe_artist, safe_title)
                folder_display = f"{safe_artist}/{safe_title}"

            os.makedirs(folder_path, exist_ok=True)
            out_path = os.path.join(folder_path, "cover.jpg")

            async with aiofiles.open(out_path, "wb") as f:
                await f.write(resp.content)

            self.log(f"Cover saved: {folder_display}/cover.jpg", "ok")

        except Exception as e:
            self.log(f"Cover download error: {e}", "error")

    def download_album_cover(self, album_data):
        """Download and save album cover with Artist/Album folder structure."""
        threading.Thread(
            target=self._download_album_cover_task,
            args=(album_data,),
            daemon=True,
        ).start()

    def _download_album_cover_task(self, album_data):
        asyncio.run(self._download_album_cover_task_async(album_data))

    async def _download_album_cover_task_async(self, album_data):
        try:
            title     = album_data.get("title", "Unknown")
            artist    = album_data.get("artist", "Unknown Artist")
            cover_url = album_data.get("cover", "")

            if not cover_url:
                self.log(f"No cover URL available for: {title}", "error")
                return

            self.log(f"Downloading album cover: {artist} - {title}…", "info")
            client = AsyncHttpClient("cover", timeout_s=15)
            resp = await client.get(cover_url, timeout=15)
            resp.raise_for_status()

            safe_artist = re.sub(r'[\\/*?:"<>|]', "", artist).strip()
            safe_album  = re.sub(r'[\\/*?:"<>|]', "", title).strip()
            
            # Create folder structure: Artist/Album/
            folder_path = os.path.join(self.download_dir, safe_artist, safe_album)
            os.makedirs(folder_path, exist_ok=True)
            
            out_path = os.path.join(folder_path, "cover.jpg")

            with open(out_path, "wb") as f:
                f.write(resp.content)

            self.log(f"Album cover saved: {safe_artist}/{safe_album}/cover.jpg", "ok")

        except Exception as e:
            self.log(f"Album cover download error: {e}", "error")

    # ── Bulk: cover di tutte le tracks ───────────────────────────────────────────

    def _download_all_covers_task(self, tracks_data):
        asyncio.run(self._async_download_all_covers(tracks_data))

    async def _download_all_covers_task_async(self, tracks_data):
        total   = len(tracks_data)
        success = 0
        skipped = 0

        self.log(f"Saving covers for {total} tracks…", "info")
        os.makedirs(self.download_dir, exist_ok=True)

        for i, track_data in enumerate(tracks_data, 1):
            title     = track_data.get("title", "Unknown")
            artist    = track_data.get("artist", "")
            cover_url = track_data.get("cover", "")

            if not cover_url:
                self.log(f"[{i}/{total}] No cover URL: {title} — skipped", "warn")
                skipped += 1
                continue

            try:
                client = AsyncHttpClient("cover", timeout_s=15)
                resp = await client.get(cover_url, timeout=15)
                resp.raise_for_status()

                safe_title  = re.sub(r'[\\/*?:"<>|]', "", title).strip()
                safe_artist = re.sub(r'[\\/*?:"<>|]', "", artist).strip()
                filename    = f"{safe_artist} - {safe_title}.jpg" if safe_artist else f"{safe_title}.jpg"
                out_path    = os.path.join(self.download_dir, filename)

                async with aiofiles.open(out_path, "wb") as f:
                    await f.write(resp.content)

                self.log(f"[{i}/{total}] Cover saved: {filename}", "ok")
                success += 1

            except Exception as e:
                self.log(f"[{i}/{total}] Cover error for '{title}': {e}", "error")

        self.log(f"All covers done — {success} saved, {skipped} skipped.", "ok")

    # ── Bulk: cover di tutte le tracks (VERSION ASINCRONA ULTRA-VELOCE) ──
    def download_all_covers(self, tracks_data):
        threading.Thread(target=self._run_async_covers, args=(tracks_data,), daemon=True).start()

    def _run_async_covers(self, tracks_data):
        asyncio.run(self._async_download_all_covers(tracks_data))

    async def _async_download_all_covers(self, tracks_data):
        total = len(tracks_data)
        success, skipped = 0, 0

        self.log(f"Saving covers for {total} tracks at warp speed…", "info")
        os.makedirs(self.download_dir, exist_ok=True)

        async def fetch_and_save(client, track_data, idx):
            nonlocal success, skipped
            title = track_data.get("title", "Unknown")
            artist = track_data.get("artist", "")
            cover_url = track_data.get("cover", "")

            if not cover_url:
                skipped += 1
                return

            try:
                resp = await client.get(cover_url, timeout=15)
                resp.raise_for_status()

                safe_title  = re.sub(r'[\\/*?:"<>|]', "", title).strip()
                safe_artist = re.sub(r'[\\/*?:"<>|]', "", artist).strip()
                filename    = f"{safe_artist} - {safe_title}.jpg" if safe_artist else f"{safe_title}.jpg"
                out_path    = os.path.join(self.download_dir, filename)

                async with aiofiles.open(out_path, "wb") as f:
                    await f.write(resp.content)
                
                success += 1
                self.log(f"[{idx}/{total}] Cover saved: {filename}", "ok")
            except Exception as e:
                self.log(f"[{idx}/{total}] Cover error for '{title}': {e}", "error")

        # Crea un client asincrono e avvia i download tutti assieme!
        async with httpx.AsyncClient(limits=httpx.Limits(max_connections=50)) as client:
            tasks = [fetch_and_save(client, track, i) for i, track in enumerate(tracks_data, 1)]
            await asyncio.gather(*tasks)

        self.log(f"All covers done — {success} saved, {skipped} skipped.", "ok")


    # ── Bulk: lyrics di tutte le tracks (VERSION ASINCRONA) ──
    def download_all_lyrics(self, tracks_data):
        threading.Thread(target=self._run_async_lyrics, args=(tracks_data,), daemon=True).start()

    def _run_async_lyrics(self, tracks_data):
        asyncio.run(self._async_download_all_lyrics(tracks_data))

    async def _async_download_all_lyrics(self, tracks_data):
        import aiofiles
        from .core.lyrics import fetch_lyrics_async
        
        total = len(tracks_data)
        success, skipped = 0, 0

        self.log(f"Fetching lyrics for {total} tracks concurrently…", "info")
        os.makedirs(self.download_dir, exist_ok=True)

        async def fetch_and_save_lyric(track_data, idx):
            nonlocal success, skipped
            title    = track_data.get("title", "Unknown")
            artist   = track_data.get("artist", "")
            isrc     = track_data.get("isrc", "")
            dur_ms   = track_data.get("duration_ms", 0)
            track_id = track_data.get("id", "")

            try:
                lyrics_text, provider = await fetch_lyrics_async(
                    track_name=title,
                    artist_name=artist,
                    duration_s=dur_ms // 1000 if dur_ms else 0,
                    track_id=track_id,
                    isrc=isrc,
                )

                if not lyrics_text:
                    skipped += 1
                    return

                safe_title  = re.sub(r'[\\/*?:"<>|]', "", title).strip()
                safe_artist = re.sub(r'[\\/*?:"<>|]', "", artist).strip()
                filename    = f"{safe_artist} - {safe_title}.lrc" if safe_artist else f"{safe_title}.lrc"
                out_path    = os.path.join(self.download_dir, filename)

                async with aiofiles.open(out_path, "w", encoding="utf-8") as f:
                    await f.write(lyrics_text)

                success += 1
                self.log(f"[{idx}/{total}] Lyrics saved: {filename} (via {provider})", "ok")
            except Exception as e:
                self.log(f"[{idx}/{total}] Lyrics error for '{title}': {e}", "error")

        # Scarica tutti i testi contemporaneamente
        tasks = [fetch_and_save_lyric(track, i) for i, track in enumerate(tracks_data, 1)]
        await asyncio.gather(*tasks)

        self.log(f"All lyrics done — {success} saved, {skipped} skipped.", "ok")

    # ── Lazy Loading - Anteprima track ──────────────────────────────────────

    def get_track_preview(self, track_id: str) -> str:
        """Retrieves l'URL di anteprima per una track (lazy loading).
        
        Questo metodo è invocato dalla GUI solo quando l'utente clicca su 'play' o 'preview'
        per evitare richieste di rete durante il caricamento iniziale della lista.
        
        Args:
            track_id: ID della track Spotify
            
        Returns:
            URL dell'anteprima MP3 (stringa vuota se non disponibile)
        """
        try:
            from .providers.spotify_metadata import SpotifyMetadataClient
            client = SpotifyMetadataClient()
            preview_url = client.get_track_preview(track_id)
            return preview_url or ""
        except Exception as e:
            self.log(f"Failed to fetch preview for track {track_id}: {e}", "debug")
            return ""

    # ── Phase 1: Metadata fetch ───────────────────────────────────────────────

    def fetch_metadata(self, url):
        self.current_url = url
        threading.Thread(
            target=lambda: asyncio.run(self._fetch_metadata_task(url)),
            daemon=True,
        ).start()

    async def _fetch_metadata_task(self, url):
        try:
            self.set_progress("Recupero metadati…")
            self.log(f"Analisi input: {url}", "info")
    
            # ── Rilevamento: è un URL o una query di ricerca? ──────────────────
            stripped = url.strip()
            is_url = (
                stripped.startswith("http")
                or stripped.startswith("spotify:")
            )
    
            if is_url:
                # ── Scelta client in base al dominio ───────────────────────────
                if "tidal.com" in url:
                    from .providers.tidal_metadata import TidalMetadataClient
                    client = TidalMetadataClient()
                elif "music.apple.com" in url:
                    from .providers.apple_music_metadata import AppleMusicMetadataClient
                    client = AppleMusicMetadataClient()
                else:
                    from .providers.spotify_metadata import SpotifyMetadataClient
                    client = SpotifyMetadataClient()
            else:
                # ── Ricerca testuale — sempre SpotifyMetadataClient ─────────────
                from .providers.spotify_metadata import SpotifyMetadataClient
                client = SpotifyMetadataClient()
            # Dopo aver scelto il client, prima di client.get_url()
            if not is_url:
                self.log("Ricerca testuale: usa search_provider_async.", "error")
                self.set_progress("")
                return
    
            # ── Chiamata universale ────────────────────────────────────────────
            # get_url ora restituisce (nome, tracks) OPPURE (nome, tracks, cover)
            result          = client.get_url(stripped)
            collection_name = result[0]
            tracks          = result[1]
            collection_cover = result[2] if len(result) > 2 else ""
            collection_meta  = result[3] if len(result) > 3 else {}

            cover = collection_cover or ""
            lower_url = url.lower()
            is_playlist = ("/playlist/" in lower_url) or ("list=" in lower_url and "olak5uy_" not in lower_url)
            is_artist = "/artist/" in lower_url or "spotify:artist:" in lower_url
            
            if not cover:
                if not is_playlist and not is_artist and tracks:
                    cover = getattr(tracks[0], "cover_url", "") or ""

            if not tracks:
                self.log("No tracks found at this URL.", "error")
                return

            self.current_tracks = tracks
            track_data = []
            
            # Retrieve playcount from Spotify if applicable (non-blocking)
            playcount_map = {}
            if "spotify.com" in url:
                try:
                    from .core.spotfetch import SpotifyWebClient
                    sp_client = SpotifyWebClient()
                    
                    try:
                        # Initialize with timeout (5 seconds)
                        sp_client.initialize()
                        
                        # Try to extract playlist / track / artist info from URL
                    
                        playlist_match = re.search(r'playlist[:/]([a-zA-Z0-9]+)', url)
                        track_match = re.search(r'track[:/]([a-zA-Z0-9]+)', url)
                        album_match = re.search(r'album[:/]([A-Za-z0-9]+)', url)
                        lower_url = url.lower()
                        is_artist = "/artist/" in lower_url or "spotify:artist:" in lower_url

                        if playlist_match:
                            self.log("Attempting to fetch playcount…", "info")
                            playlist_id = playlist_match.group(1)
                            playcount_map = sp_client.get_playlist_stats(playlist_id)
                        elif track_match and len(tracks) == 1 and not is_artist:
                            self.log("Attempting to fetch playcount…", "info")
                            track_id = track_match.group(1)
                            stats = sp_client.get_track_stats(track_id)
                            if stats.get('playcount'):
                                playcount_map[track_id] = stats.get('playcount')
                        elif album_match:
                            self.log("Attempting to fetch playcount for album (fast mode)…", "info")
                            album_id = album_match.group(1)
                            playcount_map = sp_client.get_album_stats(album_id)
                        elif is_artist:
                            playcount_map = {}
                        else:
                            pass
                    except Exception as auth_err:
                        self.log(f"Playcount unavailable: {type(auth_err).__name__}", "info")
                        
                except Exception:
                    pass  # Silently skip playcount on any error

            # ── Ciclo estrazione metadati potenziato ──
            for i, t in enumerate(tracks):
                track_id = getattr(t, 'id', '')
                
                # Cerca di retrievesre i dati in modo dinamico
                title   = getattr(t, 'title', getattr(t, 'name', f'Track {i+1}'))
                # Gestisce sia se 'artists' è una stringa che una lista
                raw_art = getattr(t, 'artists', getattr(t, 'artist', 'Unknown'))
                artist  = ", ".join(raw_art) if isinstance(raw_art, list) else str(raw_art)
                
                # Prendi l'album se esiste
                album = getattr(t, 'album', getattr(t, 'album_name', '—'))
                
                _pc_val = playcount_map.get(track_id, '') if playcount_map else ''
                playcount = _pc_val.get('playcount', '') if isinstance(_pc_val, dict) else _pc_val
                if not playcount or playcount == "0":
                    fallback_plays = getattr(t, 'plays', '')
                    playcount = fallback_plays if fallback_plays and fallback_plays != "0" else ""

                track_data.append({
                    "index":        i,
                    "id":           track_id,
                    "title":        title,
                    "artist":       artist,
                    "album":        album,
                    "cover":        getattr(t, 'cover_url', ''),
                    "duration_ms":  getattr(t, 'duration_ms', 0),
                    "explicit":     getattr(t, 'is_explicit', False),
                    "isrc":         getattr(t, 'isrc', ''),
                    "external_url": getattr(t, 'external_url', ''),
                    "preview_url":  getattr(t, 'preview_url', ''),
                    "playcount":    playcount,
                    "release_date": getattr(t, 'release_date', ''), 
                    "copyright":    getattr(t, 'copyright', ''),
                })

            badge = f"FLAC — {len(tracks)} tracks" if len(tracks) > 1 else "FLAC"

            # For artist URLs show only artist name
            lower_url = url.lower()
            is_artist = "/artist/" in lower_url or "spotify:artist:" in lower_url
            if is_artist:
                display_title  = collection_name
                display_artist = ""
            else:
                display_title  = collection_name
                display_artist = tracks[0].artists if tracks else ""

            if is_artist:
                self.set_metadata(
                    display_title,
                    display_artist,
                    cover,
                    badge,
                    artist_listeners=collection_meta.get("listeners"),
                    artist_rank=collection_meta.get("rank"),
                    artist_verified=collection_meta.get("verified", False),
                    artist_biography=collection_meta.get("biography", ""),
                )
            else:
                self.set_metadata(
                    display_title,
                    display_artist,
                    cover,
                    badge,
                    playlist_description=collection_meta.get("description"),
                    playlist_followers=collection_meta.get("followers"),
                    playlist_owner=collection_meta.get("owner", ""),
                    playlist_owner_avatar=collection_meta.get("owner_avatar", ""),
                    source=collection_meta.get("source", ""),
                    release_date=collection_meta.get("release_date"),
                    track_count=collection_meta.get("track_count"),
                )

            self.log(f"Found: {collection_name} ({len(tracks)} track(s)). Choose songs to download.", "ok")
            self.set_progress("Ready for download.")

            try:
                from .core.session_memory import add_url_to_history_async
                _lower = url.lower()
                if '/track/' in _lower or _lower.startswith('spotify:track:') or 'watch?v=' in _lower or 'youtu.be' in _lower:
                    _url_type = 'track'
                elif '/album/' in _lower or _lower.startswith('spotify:album:'):
                    _url_type = 'album'
                elif '/playlist/' in _lower or _lower.startswith('spotify:playlist:') or ('list=' in _lower and 'olak5uy_' not in _lower):
                    _url_type = 'playlist'
                elif '/artist/' in _lower or _lower.startswith('spotify:artist:'):
                    _url_type = 'artist'
                else:
                    _url_type = ''
                
                _artist = getattr(tracks[0], 'artists', '') if tracks and _url_type == 'track' else ''
                await add_url_to_history_async(url, label=collection_name, cover=cover,
                                              track_count=len(tracks), url_type=_url_type, artist=_artist)
            except Exception:
                pass

            try:
                if self._window:
                    self._window.evaluate_js(f"window.showTracklist({json.dumps(track_data)});")
            except Exception:
                pass

        except Exception as e:
            self.log(f"Error fetching metadata: {str(e)}", "error")
            self.set_progress("Error.")

    # ── Phase 2: Download ─────────────────────────────────────────────────────

    def download_tracks(self, selected_indices, config):
        threading.Thread(target=self._download_task,
                         args=(selected_indices, config), daemon=True).start()

    def _download_task(self, selected_indices, config):
        sf_logger = logging.getLogger("SpotiFLAC")
        handler   = UILogHandler(self)
        handler.setFormatter(logging.Formatter("[%(name)s] %(message)s"))
        sf_logger.addHandler(handler)
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(logging.Formatter("  %(message)s"))
        sf_logger.addHandler(console_handler)
        monitor_stop = None
        monitor_thread = None

        log_level_str    = config.get("log_level", "INFO")
        current_log_level = logging.DEBUG if log_level_str == "DEBUG" else logging.INFO
        sf_logger.setLevel(current_log_level)

        try:
            os.makedirs(self.download_dir, exist_ok=True)

            quality               = config.get("quality", "LOSSLESS")
            allow_fallback        = config.get("allow_fallback", True)
            embed_lyrics          = config.get("lyrics", True)
            enrich_metadata       = config.get("enrich_metadata", True)
            services              = config.get("services", ["tidal", "qobuz", "deezer"])
            filename_format       = config.get("filename_format", "{title} - {artist}")
            use_track_numbers     = config.get("use_track_numbers", False)
            use_album_track_numbers = config.get("use_album_track_numbers", False)
            use_artist_subfolders = config.get("use_artist_subfolders", False)
            use_album_subfolders  = config.get("use_album_subfolders", False)
            first_artist_only     = config.get("first_artist_only", False)
            lyrics_providers      = config.get("lyrics_providers") or ["spotify", "apple", "musixmatch", "lrclib", "amazon"]
            enrich_providers      = config.get("enrich_providers") or ["deezer", "apple", "qobuz", "tidal", "soundcloud"]
            track_max_retries     = int(config.get("track_max_retries", 0))
            post_download_action  = config.get("post_download_action", "none")
            post_download_command = config.get("post_download_command", "")
            qobuz_local_api_url   = config.get("qobuz_local_api_url") or None
            tidal_custom_api      = config.get("tidal_custom_api") or None
            loop_val              = config.get("loop", None)
            loop_minutes          = int(loop_val) if loop_val else None

            if not services:
                self.log("Error: select at least one service.", "error")
                return

            if len(selected_indices) == len(self.current_tracks):
                urls_to_download = [self.current_url]
                self.log("Downloading entire album/playlist…", "info")
            else:
                urls_to_download = []
                for i in selected_indices:
                    t = self.current_tracks[i]
                    t_url = getattr(t, 'external_url', None) or getattr(t, 'url', None)
                    t_id  = getattr(t, 'id', None)
                    if not t_url and t_id:
                        if "spotify" in self.current_url:
                            t_url = f"https://open.spotify.com/track/{t_id}"
                        elif "tidal" in self.current_url:
                            t_url = f"https://tidal.com/browse/track/{t_id}"
                        elif "apple" in self.current_url:
                            t_url = f"https://music.apple.com/track/{t_id}"
                    if t_url:
                        urls_to_download.append(t_url)
                    else:
                        self.log(f"Could not resolve URL for '{t.title}'. Skipping.", "error")

            if not urls_to_download:
                self.log("No valid URLs to download.", "error")
                return

            self.set_progress(f"Downloading ({quality})…")
            monitor_stop = threading.Event()
            monitor_thread = threading.Thread(
                target=self._download_stats_monitor,
                args=(monitor_stop,), daemon=True
            )
            monitor_thread.start()

            from . import SpotiFLAC

            for u in urls_to_download:
                SpotiFLAC(
                    url                     = u,
                    output_dir              = self.download_dir,
                    services                = services,
                    quality                 = quality,
                    allow_fallback          = allow_fallback,
                    filename_format         = filename_format,
                    use_track_numbers       = use_track_numbers,
                    use_album_track_numbers = use_album_track_numbers,
                    use_artist_subfolders   = use_artist_subfolders,
                    use_album_subfolders    = use_album_subfolders,
                    first_artist_only       = first_artist_only,
                    embed_lyrics            = embed_lyrics,
                    lyrics_providers        = lyrics_providers,
                    enrich_metadata         = enrich_metadata,
                    enrich_providers        = enrich_providers,
                    qobuz_local_api_url     = qobuz_local_api_url,
                    tidal_custom_api        = tidal_custom_api,
                    track_max_retries       = track_max_retries,
                    post_download_action    = post_download_action,
                    post_download_command   = post_download_command,
                    log_level               = current_log_level,
                    loop                    = loop_minutes,
                )

            self._push_download_stats()
            self.set_progress("Complete!")
            self.log(f"All tracks saved to: {self.download_dir}", "ok")
            try:
                if self._window:
                    self._window.evaluate_js("window.app_download_finished(true);")
            except Exception:
                pass

        except Exception as e:
            self.log(f"Download error: {str(e)}", "error")
            self.set_progress("Error.")
            self._push_download_stats()
            try:
                if self._window:
                    self._window.evaluate_js("window.app_download_finished(false);")
            except Exception:
                pass
        finally:
            if monitor_stop is not None:
                monitor_stop.set()
            if monitor_thread is not None:
                monitor_thread.join(timeout=1)
            self._push_download_stats()
            sf_logger.removeHandler(handler)
            if 'console_handler' in locals():
                sf_logger.removeHandler(console_handler)

    # ── Health Check ──────────────────────────────────────────────────────────

    def run_health_check(self, services):
        threading.Thread(
            target=self._health_check_task,
            args=(services,),
            daemon=True,
        ).start()

    def _download_stats_monitor(self, stop_event):
        try:
            from .core.progress import DownloadManager
            manager = DownloadManager()
            while not stop_event.wait(0.25):
                self._push_download_stats(manager.get_stats_sync())
        except Exception:
            pass
        finally:
            self._push_download_stats()

    def _push_download_stats(self, stats=None):
        try:
            if stats is None:
                from .core.progress import DownloadManager
                stats = DownloadManager().get_stats_sync()
            safe = json.dumps(stats)
            if self._window:
                self._window.evaluate_js(f"window.app_update_download_stats({safe});")
        except Exception:
            pass

    def _health_check_task(self, services):
        try:
            from .core.health_check import run_health_check as hc_run
            self.log(f"Health check started for: {', '.join(services)}", "info")
            results = asyncio.run(hc_run(services))
            data = [
                {
                    "provider": r.provider,
                    "method":   r.method,
                    "url":      r.url,
                    "ok":       r.ok,
                    "latency":  round(r.latency) if r.latency >= 0 else -1,
                    "detail":   r.detail,
                }
                for r in results
            ]
            ok_providers = [r.provider for r in results if r.ok]
            self.log(
                f"Health check — {len([r for r in results if r.ok])}/{len(results)} endpoints OK.",
                "ok" if ok_providers else "error",
            )
            try:
                if self._window:
                    self._window.evaluate_js(f"window.updateHealthResults({json.dumps(data)});")
            except Exception:
                pass
        except ImportError:
            self.log("health_check module not found.", "error")
        except Exception as e:
            self.log(f"Health check error: {str(e)}", "error")


def run_gui():
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    logging.getLogger('pywebview').setLevel(logging.WARNING)
    api = SpotiFLAC_API()

    # Try several candidate locations for the frontend files to be robust
    candidates = []

    # 1) frontend next to the installed module file (site-packages/frontend)
    site_packages_frontend = os.path.join(os.path.dirname(__file__), 'frontend', 'index.html')
    candidates.append(site_packages_frontend)

    # 2) frontend inside the backend package (if present)
    try:
        import SpotiFLAC as _sp_pkg
        pkg_frontend = os.path.join(os.path.dirname(_sp_pkg.__file__), 'frontend', 'index.html')
        candidates.append(pkg_frontend)
    except Exception:
        pass

    # 3) original heuristic (parent of site-packages, e.g. lib/pythonX.Y/frontend)
    original_frontend = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'frontend', 'index.html'))
    candidates.append(original_frontend)

    # 4) installation data dir (some installers place data files under the install prefix)
    try:
        import sysconfig
        data_dir = sysconfig.get_paths().get('data')
        if data_dir:
            candidates.append(os.path.join(data_dir, 'frontend', 'index.html'))
    except Exception:
        pass

    # Pick the first existing candidate
    html_path = None
    for p in candidates:
        if os.path.exists(p):
            html_path = p
            break

    if not html_path:
        raise FileNotFoundError(
            f"index.html not found. Tried: {candidates}"
        )
    
    window = webview.create_window(
        'SpotiFLAC', url=html_path, js_api=api,
        width=1300, height=850, 
        min_size=(1000, 800),
        frameless=True, easy_drag=False, background_color='#0a0a0a'
    )
    api.set_window(window)
    window.events.loaded += api._on_loaded
    
    webview.start(http_server=True)

if __name__ == '__main__':
    run_gui()
