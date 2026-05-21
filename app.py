import webview
import threading
import json
import os
import logging
import requests as req_lib

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

    def set_window(self, window):
        self._window = window

    def _on_loaded(self):
        self.log("Python Backend connected.", "info")
        self.log(f"Default download folder: {self.download_dir}", "info")
        self.run_health_check(["tidal", "qobuz", "deezer", "apple", "soundcloud"])
        try:
            if self._window:
                self._window.evaluate_js("window.loadHistoryAndProfiles();")
        except Exception:
            pass

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

    def set_metadata(self, title, artist, cover="", quality="FLAC"):
        data = json.dumps({"title": title, "artist": artist,
                           "cover": cover, "quality": quality})
        try:
            if self._window:
                self._window.evaluate_js(f"window.app_set_metadata({data});")
        except Exception:
            pass

    # ── Profile & History API ─────────────────────────────────────────────────

    def get_history(self):
        try:
            from SpotiFLAC.core.session_memory import get_url_history
            return get_url_history()
        except Exception:
            return []

    def get_profiles(self):
        try:
            from SpotiFLAC.core.profiles import list_profiles
            return list_profiles()
        except Exception:
            return []

    def load_profile_data(self, name):
        try:
            from SpotiFLAC.core.profiles import get_profile
            return get_profile(name) or {}
        except Exception:
            return {}

    def remove_history_item(self, url):
        try:
            from SpotiFLAC.core.session_memory import remove_url_from_history
            remove_url_from_history(url)
        except Exception:
            pass

    def get_network_status(self):
        try:
            resp = req_lib.get("https://ipapi.co/json/", timeout=10)
            data = resp.json() if resp.status_code == 200 else {}
            return {
                "ip": data.get("ip", "Unavailable"),
                "country_name": data.get("country_name", "Unknown"),
                "country_code": data.get("country_code", ""),
            }
        except Exception:
            return {
                "ip": "Unavailable",
                "country_name": "Unknown",
                "country_code": "",
            }

    def save_profile_data(self, name, cfg):
        try:
            from SpotiFLAC.core.profiles import save_profile
            save_profile(name, cfg)
            self.log(f"Profile '{name}' saved successfully.", "ok")
        except Exception as e:
            self.log(f"Failed to save profile: {e}", "error")

    # ── Window controls ───────────────────────────────────────────────────────

    def WindowMinimise(self):
        if self._window:
            self._window.minimize()

    def WindowToggleMaximise(self):
        if self._window:
            self._window.toggle_fullscreen()

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
        try:
            title   = track_data.get("title", "Unknown")
            artist  = track_data.get("artist", "")
            isrc    = track_data.get("isrc", "")
            dur_ms  = track_data.get("duration_ms", 0)
            track_id = track_data.get("id", "")

            self.log(f"Fetching lyrics for: {title}…", "info")

            from SpotiFLAC.core.lyrics import fetch_lyrics
            lyrics_text, provider = fetch_lyrics(
                track_name  = title,
                artist_name = artist,
                duration_s  = dur_ms // 1000 if dur_ms else 0,
                track_id    = track_id,
                isrc        = isrc,
            )

            if not lyrics_text:
                self.log(f"No lyrics found for: {title}", "error")
                return

            import re
            safe_title  = re.sub(r'[\\/*?:"<>|]', "", title).strip()
            safe_artist = re.sub(r'[\\/*?:"<>|]', "", artist).strip()
            filename    = f"{safe_artist} - {safe_title}.lrc" if safe_artist else f"{safe_title}.lrc"
            out_path    = os.path.join(self.download_dir, filename)

            os.makedirs(self.download_dir, exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(lyrics_text)

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
        try:
            title     = track_data.get("title", "Unknown")
            artist    = track_data.get("artist", "")
            cover_url = track_data.get("cover", "")

            if not cover_url:
                self.log(f"No cover URL available for: {title}", "error")
                return

            self.log(f"Downloading cover for: {title}…", "info")

            resp = req_lib.get(cover_url, timeout=15)
            resp.raise_for_status()

            import re
            safe_title  = re.sub(r'[\\/*?:"<>|]', "", title).strip()
            safe_artist = re.sub(r'[\\/*?:"<>|]', "", artist).strip()
            filename    = f"{safe_artist} - {safe_title}.jpg" if safe_artist else f"{safe_title}.jpg"
            out_path    = os.path.join(self.download_dir, filename)

            os.makedirs(self.download_dir, exist_ok=True)
            with open(out_path, "wb") as f:
                f.write(resp.content)

            self.log(f"Cover saved: {filename}", "ok")

        except Exception as e:
            self.log(f"Cover download error: {e}", "error")

    # ── Phase 1: Metadata fetch ───────────────────────────────────────────────

    def fetch_metadata(self, url, include_featuring=False):
        self.current_url = url
        threading.Thread(
            target=self._fetch_metadata_task,
            args=(url, include_featuring),
            daemon=True,
        ).start()

    def _fetch_metadata_task(self, url, include_featuring=False):
        try:
            self.set_progress("Fetching metadata…")
            self.log(f"Analysing URL: {url}", "info")

            if "tidal.com" in url:
                from SpotiFLAC.providers.tidal_metadata import TidalMetadataClient
                client = TidalMetadataClient()
            elif "music.apple.com" in url:
                from SpotiFLAC.providers.apple_music_metadata import AppleMusicMetadataClient
                client = AppleMusicMetadataClient()
            else:
                from SpotiFLAC.providers.spotify_metadata import SpotifyMetadataClient
                client = SpotifyMetadataClient()

            collection_name, tracks = client.get_url(url, include_featuring=include_featuring)

            if not tracks:
                self.log("No tracks found at this URL.", "error")
                return

            self.current_tracks = tracks
            track_data = []
            
            # Retrieve playcount from Spotify if applicable (non-blocking)
            playcount_map = {}
            if "spotify.com" in url:
                try:
                    self.log("Attempting to fetch playcount…", "info")
                    from SpotiFLAC.core.spotfetch import SpotifyWebClient
                    sp_client = SpotifyWebClient()
                    
                    try:
                        # Initialize with timeout (5 seconds)
                        sp_client.initialize()
                        
                        # Try to extract playlist ID from URL
                        import re
                        playlist_match = re.search(r'playlist[:/]([a-zA-Z0-9]+)', url)
                        if playlist_match:
                            playlist_id = playlist_match.group(1)
                            playcount_map = sp_client.get_playlist_stats(playlist_id)
                        else:
                            # For individual tracks, get playcount per track
                            for t in tracks:
                                track_id = getattr(t, 'id', '')
                                if track_id:
                                    stats = sp_client.get_track_stats(track_id)
                                    if stats.get('playcount'):
                                        playcount_map[track_id] = stats.get('playcount')
                    except Exception as auth_err:
                        self.log(f"Playcount unavailable: {type(auth_err).__name__}", "info")
                        
                except Exception as e:
                    pass  # Silently skip playcount on any error

            for i, t in enumerate(tracks):
                track_id = getattr(t, 'id', '')
                playcount = playcount_map.get(track_id, '') if playcount_map else ''
                track_data.append({
                    "index":       i,
                    "id":          track_id,
                    "title":       getattr(t, 'title', f'Track {i+1}'),
                    "artist":      getattr(t, 'artists', ''),
                    "cover":       getattr(t, 'cover_url', ''),
                    "duration_ms": getattr(t, 'duration_ms', 0),
                    "explicit":    getattr(t, 'explicit', False),
                    "isrc":        getattr(t, 'isrc', ''),
                    "external_url": getattr(t, 'external_url', ''),
                    "playcount":   playcount,
                })

            badge = f"FLAC — {len(tracks)} tracks" if len(tracks) > 1 else "FLAC"

            # For artist URLs show only artist name
            lower_url = url.lower()
            is_artist = "/artist/" in lower_url
            if is_artist:
                display_title  = collection_name
                display_artist = ""
            else:
                display_title  = collection_name
                display_artist = tracks[0].artists if tracks else ""

            self.set_metadata(display_title, display_artist,
                              tracks[0].cover_url if tracks else "", badge)

            self.log(f"Found: {collection_name} ({len(tracks)} track(s)). Choose songs to download.", "ok")
            self.set_progress("Ready for download.")

            try:
                from SpotiFLAC.core.session_memory import add_url_to_history
                cover = getattr(tracks[0], 'cover_url', '') if tracks else ''
                add_url_to_history(url, label=collection_name, cover=cover)
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
            include_featuring     = config.get("include_featuring", False)
            lyrics_providers      = config.get("lyrics_providers") or ["spotify", "apple", "musixmatch", "lrclib", "amazon"]
            enrich_providers      = config.get("enrich_providers") or ["deezer", "apple", "qobuz", "tidal", "soundcloud"]
            track_max_retries     = int(config.get("track_max_retries", 0))
            post_download_action  = config.get("post_download_action", "none")
            post_download_command = config.get("post_download_command", "")
            qobuz_token           = config.get("qobuz_token") or None
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

            from SpotiFLAC import SpotiFLAC

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
                    include_featuring       = include_featuring,
                    embed_lyrics            = embed_lyrics,
                    lyrics_providers        = lyrics_providers,
                    enrich_metadata         = enrich_metadata,
                    enrich_providers        = enrich_providers,
                    qobuz_token             = qobuz_token,
                    tidal_custom_api        = tidal_custom_api,
                    track_max_retries       = track_max_retries,
                    post_download_action    = post_download_action,
                    post_download_command   = post_download_command,
                    log_level               = current_log_level,
                    loop                    = loop_minutes,
                )

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

    # ── Health Check ──────────────────────────────────────────────────────────

    def run_health_check(self, services):
        threading.Thread(
            target=self._health_check_task,
            args=(services,),
            daemon=True,
        ).start()

    def _download_stats_monitor(self, stop_event):
        try:
            from SpotiFLAC.core.progress import DownloadManager
            manager = DownloadManager()
            while not stop_event.wait(0.25):
                self._push_download_stats(manager.get_stats())
        except Exception:
            pass
        finally:
            self._push_download_stats()

    def _push_download_stats(self, stats=None):
        try:
            if stats is None:
                from SpotiFLAC.core.progress import DownloadManager
                stats = DownloadManager().get_stats()
            safe = json.dumps(stats)
            if self._window:
                self._window.evaluate_js(f"window.app_update_download_stats({safe});")
        except Exception:
            pass

    def _health_check_task(self, services):
        try:
            import importlib
            hc_module = importlib.import_module("SpotiFLAC.core.health_check")
            hc_run    = getattr(hc_module, "run_health_check")
            self.log(f"Health check started for: {', '.join(services)}", "info")
            results = hc_run(services)
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
    api = SpotiFLAC_API()
    html_path = os.path.join(os.path.dirname(__file__), 'index.html')
    window = webview.create_window(
        'SpotiFLAC', url=html_path, js_api=api,
        width=1300, height=850, min_size=(650, 580),
        frameless=True, easy_drag=False, background_color='#0a0a0a'
    )
    api.set_window(window)
    webview.start(http_server=True)

if __name__ == '__main__':
    run_gui()