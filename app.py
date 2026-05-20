import webview
import threading
import json
import os
import logging

# ── Default download folder ───────────────────────────────────────────────────
DEFAULT_DOWNLOAD_DIR = os.path.join(os.path.expanduser("~"), "Music", "SpotiFLAC")

# ── Logging handler → UI ─────────────────────────────────────────────────────
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
        
        # 1. Avvia l'health check in automatico all'avvio
        self.run_health_check(["tidal", "qobuz", "deezer", "apple", "soundcloud"])
        
        # 2. Richiedi all'UI di caricare storia e profili salvati in Python
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

    # ── API per Profili e Cronologia ──────────────────────────────────────────

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

    def save_profile_data(self, name, cfg):
        try:
            from SpotiFLAC.core.profiles import save_profile
            save_profile(name, cfg)
            self.log(f"Profile '{name}' saved successfully.", "ok")
        except Exception as e:
            self.log(f"Failed to save profile: {e}", "error")

    # ── Window and folder controls ────────────────────────────────────────────

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
                except:
                    pass

    def open_url(self, url):
        import webbrowser
        webbrowser.open(url)

    # ── Phase 1: Metadata and track lookup ───────────────────────────────────

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

            # Estrazione approfondita dei dati per la tabella e per le INFO (Punti 1 e 2)
            for i, t in enumerate(tracks):
                track_data.append({
                    "index": i,
                    "title": getattr(t, 'title', f'Track {i+1}'),
                    "artist": getattr(t, 'artists', ''),
                    "cover": getattr(t, 'cover_url', ''),
                    "duration_ms": getattr(t, 'duration_ms', getattr(t, 'duration', 0)),
                    "explicit": getattr(t, 'explicit', False),
                    "isrc": getattr(t, 'isrc', ''),
                    "url": getattr(t, 'url', getattr(t, 'link', '')),
                    "plays": getattr(t, 'plays', getattr(t, 'play_count', 0))
                })

            badge = f"FLAC — {len(tracks)} tracks" if len(tracks) > 1 else "FLAC"
            
            # Gestione nome per i link dell'artista (Punto 3)
            if "/artist/" in url.lower() or "artist" in url.lower():
                display_title = collection_name
                display_artist = ""
            else:
                display_title = collection_name
                display_artist = tracks[0].artists if tracks else ""

            self.set_metadata(display_title, display_artist, tracks[0].cover_url if tracks else "", badge)

            self.log(f"Found: {collection_name} ({len(tracks)} track(s)). Choose the songs to download.", "ok")
            self.set_progress("Ready for download.")

            # Pass track data back to UI to show the list
            try:
                if self._window:
                    self._window.evaluate_js(f"window.showTracklist({json.dumps(track_data)});")
            except Exception:
                pass

        except Exception as e:
            self.log(f"Error fetching metadata: {str(e)}", "error")
            self.set_progress("Error.")

    # ── Phase 2: Actual download ──────────────────────────────────────────────

    def download_tracks(self, selected_indices, config):
        threading.Thread(target=self._download_task, args=(selected_indices, config), daemon=True).start()

    def _download_task(self, selected_indices, config):
        sf_logger = logging.getLogger("SpotiFLAC")
        handler   = UILogHandler(self)
        handler.setFormatter(logging.Formatter("[%(name)s] %(message)s"))
        sf_logger.addHandler(handler)

        log_level_str = config.get("log_level", "INFO")
        current_log_level = logging.DEBUG if log_level_str == "DEBUG" else logging.INFO
        sf_logger.setLevel(current_log_level)

        try:
            os.makedirs(self.download_dir, exist_ok=True)

            quality              = config.get("quality", "LOSSLESS")
            allow_fallback       = config.get("allow_fallback", True)
            embed_lyrics         = config.get("lyrics", True)
            enrich_metadata      = config.get("enrich_metadata", True)
            services             = config.get("services", ["tidal", "qobuz", "deezer"])

            filename_format      = config.get("filename_format", "{title} - {artist}")
            use_track_numbers    = config.get("use_track_numbers", False)
            use_album_track_numbers = config.get("use_album_track_numbers", False)
            use_artist_subfolders = config.get("use_artist_subfolders", False)
            use_album_subfolders  = config.get("use_album_subfolders", False)
            first_artist_only    = config.get("first_artist_only", False)
            include_featuring    = config.get("include_featuring", False)

            lyrics_providers     = config.get("lyrics_providers") or ["spotify", "apple", "musixmatch", "lrclib", "amazon"]
            enrich_providers     = config.get("enrich_providers") or ["deezer", "apple", "qobuz", "tidal", "soundcloud"]

            track_max_retries    = int(config.get("track_max_retries", 0))
            post_download_action = config.get("post_download_action", "none")
            post_download_command = config.get("post_download_command", "")
            qobuz_token          = config.get("qobuz_token") or None
            tidal_custom_api     = config.get("tidal_custom_api") or None

            loop_val             = config.get("loop", None)
            loop_minutes         = int(loop_val) if loop_val else None

            if not services:
                self.log("Error: you must select at least one service/source.", "error")
                return

            # ── Costruzione corretta degli URL per le singole tracce ──────────
            if len(selected_indices) == len(self.current_tracks):
                urls_to_download = [self.current_url]
                self.log("Starting download of the entire album/playlist…", "info")
            else:
                urls_to_download = []
                for i in selected_indices:
                    t = self.current_tracks[i]
                    t_url = getattr(t, 'url', None) or getattr(t, 'link', None)
                    t_id = getattr(t, 'id', None) or getattr(t, 'track_id', None)
                    
                    if not t_url and t_id:
                        if "spotify" in self.current_url:
                            t_url = f"https://open.spotify.com/track/{t_id}"
                        elif "tidal" in self.current_url:
                            t_url = f"https://tidal.com/browse/track/{t_id}"
                        elif "apple" in self.current_url:
                            t_url = f"https://music.apple.com/track/{t_id}"
                        elif "deezer" in self.current_url:
                            t_url = f"https://www.deezer.com/track/{t_id}"
                        
                    if t_url:
                        urls_to_download.append(t_url)
                    else:
                        self.log(f"Could not resolve URL for '{t.title}'. It will be skipped.", "error")

            if not urls_to_download:
                self.log("No valid URLs to download.", "error")
                return

            self.set_progress(f"Downloading ({quality})…")

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
            self.log(f"All selected tracks saved to: {self.download_dir}", "ok")

        except Exception as e:
            self.log(f"Download error: {str(e)}", "error")
            self.set_progress("Error.")
        finally:
            sf_logger.removeHandler(handler)

    # ── Health Check ─────────────────────────────────────────────────────────

    def run_health_check(self, services):
        threading.Thread(
            target=self._health_check_task,
            args=(services,),
            daemon=True,
        ).start()

    def _health_check_task(self, services):
        try:
            import importlib
            hc_module = importlib.import_module("SpotiFLAC.core.health_check")
            hc_run = getattr(hc_module, "run_health_check")
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
                f"Health check complete — {len([r for r in results if r.ok])}/{len(results)} endpoints OK.",
                "ok" if ok_providers else "error",
            )
            # Pass results to UI
            try:
                if self._window:
                    self._window.evaluate_js(f"window.updateHealthResults({json.dumps(data)});")
            except Exception:
                pass
        except ImportError:
            self.log("health_check module not found. Make sure SpotiFLAC is installed.", "error")
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