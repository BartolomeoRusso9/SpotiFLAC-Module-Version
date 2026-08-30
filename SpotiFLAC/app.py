from __future__ import annotations

import contextlib
import importlib.metadata
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import webview

from .api_mixins.covers_lyrics import CoversLyricsMixin
from .api_mixins.csv_import import CsvImportMixin
from .api_mixins.dedup import DedupMixin
from .api_mixins.discovery import DiscoveryMixin
from .api_mixins.extension_health import ExtensionHealthMixin
from .api_mixins.local_tagging import LocalTaggingMixin
from .api_mixins.stats import StatsMixin
from .api_mixins.subscriptions import SubscriptionsMixin
from .api_mixins.trust import TrustMixin
from .core.http import AsyncHttpClient
from .core.loop_runner import run_sync
from .core.url_utils import url_host_matches

DEFAULT_DOWNLOAD_DIR = os.path.join(os.path.expanduser("~"), "Music", "SpotiFLAC")

# Opt-in required before the GUI/web bridge may run post_download_action
# ="command" (see _post_command_allowed / _download_task).
POST_COMMAND_ENV = "SPOTIFLAC_ALLOW_POST_COMMAND"

#: Playcount lookups run one request per track against Spotify's internal
#: GraphQL API. These two together cap that at ~12 requests/second — roughly
#: 2.5 minutes for a 1800-track CSV, in the background — instead of the ~32/s
#: an unspaced pool of 8 produced. See _fetch_track_playcounts().
PLAYCOUNT_WORKERS = 4
PLAYCOUNT_MIN_INTERVAL_S = 0.08


def _post_command_allowed() -> bool:
    """Whether this process will let a *bridge* caller run a shell command
    as a post-download action.

    Off by default, and deliberately not something the caller can turn on
    for itself. Every method on this class is reachable two ways: from the
    desktop window via pywebview, and — in `--web` mode — as an HTTP
    endpoint (see webapp.py's ALLOWED_METHODS), where the "caller" may be
    anyone who can reach the port and the config dict is just a JSON body.
    Reading a shell command out of that dict makes the two indistinguishable,
    so the decision is moved somewhere only whoever started the process can
    reach: this environment variable.

    The CLI is unaffected — `--post-action command` there goes straight to
    client.SpotiFLAC() without passing through this class, and someone who
    can already type a shell command into their own shell gains nothing by
    typing it into ours.
    """
    return os.environ.get(POST_COMMAND_ENV, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "y",
    )


class UILogHandler(logging.Handler):
    """Mirrors the `SpotiFLAC` logger into the GUI's Logs panel.

    Every record still reaches the panel; what changes with the level is
    whether it also raises a toast. INFO used to map to "info", which
    toasts — so a single album download popped one notification per
    library log line ("[SpotiFLAC.core.spotify_metadata] [DEBUG] URL
    type: ...", "[ExtMgr] 'x' already up-to-date", one per merged
    segment, ...) and buried the screen in what read as debug spam.
    These are diagnostics, not things the user asked to be interrupted
    by, so INFO and DEBUG now map to "debug": panel-only. Only WARNING
    and above, which the user does need to see without opening the
    panel, still toast.
    """

    def __init__(self, api) -> None:
        super().__init__()
        self.api = api

    def emit(self, record) -> None:
        try:
            msg = self.format(record)
            if record.levelno >= logging.ERROR:
                ltype = "error"
            elif record.levelno >= logging.WARNING:
                ltype = "warn"
            else:
                ltype = "debug"
            self.api.log(msg, ltype)
        except Exception:
            pass


class SpotiFLAC_API(
    LocalTaggingMixin,
    CoversLyricsMixin,
    DiscoveryMixin,
    DedupMixin,
    TrustMixin,
    SubscriptionsMixin,
    ExtensionHealthMixin,
    CsvImportMixin,
    StatsMixin,
):
    """pywebview/`--web` bridge — every method here (plus the two mixins
    above) becomes a callable the frontend invokes as `pywebview.api.<name>`
    (desktop) or `POST /api/<name>` (web mode, see webapp.py's
    ALLOWED_METHODS). Both entry points bind to *this one object*, which is
    why splitting it means moving method bodies into mixins rather than into
    separate top-level objects — see api_mixins/__init__.py for the reasoning
    and api_mixins/local_tagging.py + api_mixins/covers_lyrics.py for what's
    moved out so far. What's still here (search, metadata fetch, the actual
    download orchestration, settings/profiles/registries, window/folder
    plumbing) is intentionally left in place for now rather than split in
    the same pass — those are the highest-traffic, most interdependent
    methods, and this environment can't launch the real pywebview window or
    click through the web GUI to fully exercise a move that size.
    """

    def __init__(self) -> None:
        self._window = None
        # Set for as long as a download is running. Background work that is
        # merely nice to have — the playcount column — waits on this rather
        # than competing with the download for the same API and IP.
        self._download_active = threading.Event()
        # Optional callable set by webapp.py in web mode: fn(event_name, args_list).
        # Desktop (pywebview) mode never sets this and is completely unaffected.
        self._ws_broadcast = None
        self.download_dir = DEFAULT_DOWNLOAD_DIR
        # Which account this instance belongs to, set by webapp.py's
        # ApiRegistry in multi-user mode and left blank everywhere else. It
        # is what the download log is written under, which is what per-account
        # quotas count and what the dashboard reads back.
        self.owner = ""
        self.current_tracks = []
        self.current_url = ""
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

    def set_window(self, window) -> None:
        self._window = window

    def _push(self, fn_name: str, *args) -> None:
        """Sends a frontend event, either as a pywebview evaluate_js call
        (desktop) or as a structured WebSocket message (web mode via
        webapp.py). Exactly one of the two paths runs depending on which
        mode the app was launched in.
        """
        if self._window:
            try:
                js_args = ", ".join(json.dumps(a) for a in args)
                self._window.evaluate_js(f"window.{fn_name}({js_args});")
            except Exception:
                pass
        if self._ws_broadcast:
            try:
                self._ws_broadcast(fn_name, list(args))
            except Exception:
                pass

    def _on_loaded(self) -> None:
        """Initializes the frontend after the webview finishes loading.

        Starts extension initialization and updates the frontend with stored history, profiles, and the application version when a window is available.

        Everything logged here is a startup diagnostic, so it goes out as
        "debug": it lands in the Logs view but raises no toast. A stack of
        six toasts on every launch told the user nothing they had asked
        for. Only the ffmpeg/Node *failure* paths below stay at "error",
        because those do need to interrupt.
        """
        self.log("Python Backend connected.", "debug")
        self.log(f"Default download folder: {self.download_dir}", "debug")
        self._check_ffmpeg_startup()
        self._check_node_startup()
        try:
            from .extensions.manager import ExtensionManager

            self.log("Download extension...", "debug")
            threading.Thread(
                target=lambda: ExtensionManager(auto_install_downloads=True),
                daemon=True,
            ).start()
        except Exception as e:
            self.log(f"Error starting extensions: {e}", "warn")
        app_version = self.app_version
        try:
            self._push("loadHistoryAndProfiles")
            self._push("__set_version_label", app_version)
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
                    "https://api.github.com/repos/BartolomeoRusso9/SpotiFLAC-Module-Version/releases/latest",
                    headers={
                        "Accept": "application/vnd.github.v3+json",
                        "User-Agent": "SpotiFLAC-Desktop",
                    },
                    timeout=10,
                )
                if resp.status_code != 200:
                    return {"latest_version": "", "published_at": ""}
                data = resp.json() or {}
                return {
                    "latest_version": str(data.get("tag_name", "") or "")
                    .lstrip("v")
                    .strip(),
                    "published_at": str(data.get("published_at", "") or ""),
                }
            except Exception:
                return {"latest_version": "", "published_at": ""}

        return run_sync(_inner())

    def _check_ffmpeg_startup(self) -> None:
        try:
            from .core.ffmpeg_check import check_ffmpeg

            result = check_ffmpeg()
            if result["available"]:
                short = result["version"][:80]
                self.log(f"ffmpeg: {short}", "debug")
            else:
                self.log(
                    "⚠  ffmpeg not found — Tidal FLAC muxing and Amazon "
                    "decryption will fail. MP3 transcoding will try to install "
                    "it automatically the first time you use it; otherwise "
                    "install: https://ffmpeg.org/download.html",
                    "error",
                )
                try:
                    self._push("showFfmpegWarning", result)
                except Exception:
                    pass
        except Exception as exc:
            self.log(f"ffmpeg check error: {exc}", "warn")

    def _check_node_startup(self) -> None:
        # Informational only — this never attempts to install Node itself;
        # that only happens lazily, the first time a JS extension actually
        # runs (see extensions/runtime.py's JSRuntime.start() /
        # core/node_check.py's ensure_node_installed()).
        try:
            from .core.node_check import check_node

            result = check_node()
            if result["available"]:
                self.log(f"Node.js: {result['version']}", "debug")
            else:
                self.log(
                    "⚠  Node.js not found — JavaScript extensions won't work "
                    "until it's installed. SpotiFLAC will try to install it "
                    "automatically the first time you use one; see the "
                    "Extensions section of the README for the supported "
                    "package managers, or install it yourself: "
                    "https://nodejs.org/en/download",
                    "error",
                )
                try:
                    self._push("showNodeWarning", result)
                except Exception:
                    pass
        except Exception as exc:
            self.log(f"Node.js check error: {exc}", "warn")

    def get_artist_images(self, url):
        # Not implemented: returns an empty list to trigger the JS fallback
        return []

    # ── Optional public method (JS can query it later as well) ─────────────
    def get_ffmpeg_status(self) -> dict:
        from .core.ffmpeg_check import check_ffmpeg

        return check_ffmpeg()

    def get_node_status(self) -> dict:
        from .core.node_check import check_node

        return check_node()

    # ── UI communication ──────────────────────────────────────────────────────

    def log(self, message, type="") -> None:
        """Sends one line to the frontend log.

        `type` selects both the colour in the Logs view and whether a toast
        pops: "ok"/"info"/"warn"/"error" toast, "debug" (and the empty
        default) are log-only.

        The bar for a toast is "the user would be worse off for missing
        this", not "something happened". In practice that means outcomes
        and summaries, not narration: "Downloading X…" followed by "X
        saved" is two popups for one event, and a per-item line inside a
        loop is one popup per track. Both belong at "debug" — they are
        still in the Logs view, one click away, and the closing summary
        carries the counts.

        For a list the user does want itemised in the panel — the unmatched
        rows of a CSV import, say — append "-quiet" to the type
        ("error-quiet", "warn-quiet"): the line keeps its colour there and
        raises no toast, leaving one summary toast to stand for the list.
        """
        try:
            self._push("app_log", str(message), type)
        except Exception:
            pass

    def set_progress(self, label="") -> None:
        try:
            self._push("app_set_progress", label)
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
    ) -> None:
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

        try:
            self._push("app_set_metadata", payload)
        except Exception:
            pass

    def _fetch_track_playcounts(
        self,
        sp_client,
        track_ids: list[str],
    ) -> dict[str, dict]:
        """Retrieves playcount per track in parallel using get_track_stats.

        There is no bulk form of this query — an arbitrary list of tracks
        costs one request each — so a large CSV means thousands of them, and
        SpotifyWebClient.query() has no 429 handling of its own (only a 401
        refresh). Two things keep that from turning into a burst against
        Spotify's internal API:

        - `PLAYCOUNT_MIN_INTERVAL_S` spaces request *starts* globally, no
          matter how many workers are running, so throughput is a property
          of this constant rather than of the pool size.
        - a download in progress pauses the whole thing. Playcounts are a
          column in a table; a download is what the user actually asked for,
          and the two share an IP and an origin, so the column waits.
        """
        stats_map: dict[str, dict] = {}
        unique_ids = [tid for tid in dict.fromkeys(track_ids) if tid]
        if not unique_ids:
            return stats_map

        gate = threading.Lock()
        next_start = [0.0]

        def _fetch_one(track_id: str) -> dict:
            # Yield entirely while a download is running.
            while self._download_active.is_set():
                time.sleep(0.5)
            with gate:
                now = time.monotonic()
                wait = next_start[0] - now
                if wait > 0:
                    time.sleep(wait)
                    now = time.monotonic()
                next_start[0] = now + PLAYCOUNT_MIN_INTERVAL_S
            return sp_client.get_track_stats(track_id)

        max_workers = min(PLAYCOUNT_WORKERS, len(unique_ids))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_id = {
                executor.submit(_fetch_one, track_id): track_id
                for track_id in unique_ids
            }
            for future in as_completed(future_to_id):
                track_id = future_to_id[future]
                try:
                    stats = future.result()
                    if stats.get("playcount"):
                        stats_map[track_id] = stats
                except Exception:
                    continue
        return stats_map

    # ── Profile & History API ─────────────────────────────────────────────────

    def save_settings(self, cfg: dict) -> None:
        try:
            settings_file = Path.home() / ".cache" / "spotiflac" / "gui-settings.json"
            settings_file.parent.mkdir(parents=True, exist_ok=True)
            settings_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        except Exception as e:
            self.log(f"Failed to save settings: {e}", "error")

    def save_theme(self, mode: str) -> dict:
        """Persists just the colour mode, without touching anything else.

        The theme picker applies immediately, but the rest of the Settings
        form only reaches disk when the user presses Save. Routing the theme
        through save_settings() would therefore either write half-edited
        settings or lose the theme, which is why it gets its own merge here.

        This is also what makes the choice survive a restart at all: the
        desktop window's localStorage is per-origin and has historically not
        been stable across launches (see run_gui()), so gui-settings.json is
        the copy that can be relied on.
        """
        mode = str(mode or "auto")
        if mode not in ("auto", "light", "dark"):
            return {"ok": False, "error": f"unknown theme mode: {mode}"}
        try:
            cfg = self.load_settings() or {}
            cfg["theme"] = mode
            self.save_settings(cfg)
            return {"ok": True, "theme": mode}
        except Exception as e:
            self.log(f"Failed to save theme: {e}", "error")
            return {"ok": False, "error": str(e)}

    def load_settings(self) -> dict:
        try:
            settings_file = Path.home() / ".cache" / "spotiflac" / "gui-settings.json"
            if settings_file.exists():
                return json.loads(settings_file.read_text(encoding="utf-8"))
        except Exception as e:
            self.log(f"Failed to load settings: {e}", "error")
        return {}

    # ── Extension Registry API ─────────────────────────────────────────────

    def get_registries(self) -> list | dict:
        """Returns every known extension-registry URL with its origin
        (environment variable, .env file, or added from the GUI) and
        whether it is currently enabled."""
        try:
            from .extensions import registry_config

            return registry_config.list_registries()
        except Exception as e:
            self.log(f"Failed to load registries: {e}", "error")
            return {"error": True, "message": str(e)}

    def add_registry(self, url: str) -> dict:
        try:
            from .extensions import registry_config

            registries = registry_config.add_registry(url)
            self.log(f"Registry added: {url}", "debug")
            return {"ok": True, "registries": registries}
        except Exception as e:
            self.log(f"Failed to add registry: {e}", "error")
            return {"ok": False, "error": str(e)}

    def remove_registry(self, url: str) -> dict:
        try:
            from .extensions import registry_config

            registries = registry_config.remove_registry(url)
            self.log(f"Registry removed: {url}", "debug")
            return {"ok": True, "registries": registries}
        except Exception as e:
            self.log(f"Failed to remove registry: {e}", "error")
            return {"ok": False, "error": str(e)}

    def get_history(self):
        try:
            from .core.session_memory import get_url_history_async

            return run_sync(get_url_history_async())
        except Exception:
            return []

    def get_profiles(self):
        try:
            from .core.profiles import list_profiles_async

            return run_sync(list_profiles_async())
        except Exception:
            return []

    def load_profile_data(self, name):
        try:
            from .core.profiles import get_profile_async

            return run_sync(get_profile_async(name)) or {}
        except Exception:
            return {}

    def cache_image(self, url):
        return url

    def get_spotify_home_feed(self):
        """Metodo chiamato da app.js per retrievesre l'Home Feed."""
        try:
            from .core.spotfetch import SpotifyWebClient
            from .core.spotify_metadata import parse_home_feed

            client = SpotifyWebClient()
            raw_data = client.get_home_feed()
            return parse_home_feed(raw_data)
        except Exception as e:
            import logging

            logging.exception(f"Error retrieving Home Feed: {e}")
            return {"success": False, "error": str(e)}

    def search_provider(self, query, limit=50):
        """Search music providers (Spotify) for metadata matching `query`.

        Returns a dictionary with 4 sections: tracks, albums, artists, playlists (max 50 results each).
        """
        try:
            from .core.spotify_metadata import SpotifyMetadataClient

            client = SpotifyMetadataClient()
            # client.search() already returns a dictionary with the 4 arrays
            results = client.search(query, limit=limit)

            out = {"tracks": [], "albums": [], "artists": [], "playlists": []}

            # --- Tracks ---
            for t in results.get("tracks", [])[:limit]:
                out["tracks"].append(
                    {
                        "id": getattr(t, "id", ""),
                        "name": getattr(t, "title", ""),  # Formato Go
                        "title": getattr(t, "title", ""),  # Formato Legacy
                        "type": "track",
                        "artists": getattr(t, "artists", ""),  # Formato Go
                        "artist": getattr(t, "artists", ""),  # Formato Legacy
                        "album_name": getattr(t, "album", ""),
                        "album": getattr(t, "album", ""),
                        "duration_ms": getattr(t, "duration_ms", 0),
                        "images": getattr(t, "cover_url", ""),  # Formato Go
                        "cover": getattr(t, "cover_url", ""),  # Formato Legacy
                        "external_urls": getattr(t, "external_url", ""),
                        "external_url": getattr(t, "external_url", ""),
                        "preview_url": getattr(t, "preview_url", ""),
                        "playcount": getattr(t, "plays", ""),
                        "is_explicit": getattr(t, "is_explicit", False),
                        "explicit": getattr(t, "is_explicit", False),
                        "isrc": getattr(t, "isrc", ""),
                        "provider": "spotify",
                    },
                )

            # --- Albums ---
            for a in results.get("albums", [])[:limit]:
                out["albums"].append(
                    {
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
                    },
                )

            # --- Artists ---
            for art in results.get("artists", [])[:limit]:
                out["artists"].append(
                    {
                        "id": art.get("id", ""),
                        "name": art.get("name", ""),
                        "title": art.get("name", ""),
                        "type": "artist",
                        "images": art.get("cover_url", ""),
                        "cover": art.get("cover_url", ""),
                        "external_urls": art.get("external_url", ""),
                        "external_url": art.get("external_url", ""),
                        "provider": "spotify",
                    },
                )

            # --- Playlists ---
            for p in results.get("playlists", [])[:limit]:
                out["playlists"].append(
                    {
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
                    },
                )

            return out
        except Exception as e:
            self.log(f"search_provider error: {e}", "error")
            return {"tracks": [], "albums": [], "artists": [], "playlists": []}

    def _search_provider_thread(self, query, limit) -> None:
        try:
            from .core.spotify_metadata import SpotifyMetadataClient

            client = SpotifyMetadataClient()
            results = client.search(query, limit=limit)

            out = {"tracks": [], "albums": [], "artists": [], "playlists": []}

            # --- Tracks ---
            for t in results.get("tracks", [])[:limit]:
                out["tracks"].append(
                    {
                        "id": getattr(t, "id", ""),
                        "name": getattr(t, "title", ""),
                        "title": getattr(t, "title", ""),
                        "type": "track",
                        "artists": getattr(t, "artists", ""),
                        "artist": getattr(t, "artists", ""),
                        "album_name": getattr(t, "album", ""),
                        "album": getattr(t, "album", ""),
                        "duration_ms": getattr(t, "duration_ms", 0),
                        "images": getattr(t, "cover_url", ""),
                        "cover": getattr(t, "cover_url", ""),
                        "external_urls": getattr(t, "external_url", ""),
                        "external_url": getattr(t, "external_url", ""),
                        "preview_url": getattr(t, "preview_url", ""),
                        "playcount": getattr(t, "plays", ""),
                        "is_explicit": getattr(t, "is_explicit", False),
                        "explicit": getattr(t, "is_explicit", False),
                        "isrc": getattr(t, "isrc", ""),
                        "provider": "spotify",
                    },
                )

            # --- Albums ---
            for a in results.get("albums", [])[:limit]:
                out["albums"].append(
                    {
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
                    },
                )

            # --- Artists ---
            for art in results.get("artists", [])[:limit]:
                out["artists"].append(
                    {
                        "id": art.get("id", ""),
                        "name": art.get("name", ""),
                        "title": art.get("name", ""),
                        "type": "artist",
                        "images": art.get("cover_url", ""),
                        "cover": art.get("cover_url", ""),
                        "external_urls": art.get("external_url", ""),
                        "external_url": art.get("external_url", ""),
                        "provider": "spotify",
                    },
                )

            # --- Playlists ---
            for p in results.get("playlists", [])[:limit]:
                out["playlists"].append(
                    {
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
                    },
                )

            try:
                # The JS will now receive a complete object as in the Go version
                self._push("app_handle_provider_search_results", out)
            except Exception:
                pass
        except Exception as e:
            try:
                self._push("app_handle_provider_search_error", str(e))
            except Exception:
                pass

    def search_provider_async(self, query, limit=50):  # Limite di default updated a 50
        if not query:
            return {"status": "empty"}
        threading.Thread(
            target=self._search_provider_thread,
            args=(query, limit),
            daemon=True,
        ).start()
        return {"status": "started"}

    def search_code(self, query, path=".", limit=200):
        """Search the SpotiFLAC package source (substring, case-insensitive).

        Returns list of {path, line, snippet}.

        `path` is confined to the installed package directory. This returns
        matching *lines*, not just filenames, so an unconstrained path would
        make it a general "read any file the process can open" primitive —
        and every public method here is callable from whatever JS is running
        in the window (pywebview binds the whole object as js_api).
        """
        try:
            from .core.code_search import search_code

            root = Path(__file__).resolve().parent
            requested = (root / (path or ".")).resolve()
            if requested != root and root not in requested.parents:
                self.log(
                    f"search_code: '{path}' is outside the package directory",
                    "error",
                )
                return []

            return search_code(query, path=str(requested), limit=limit or 200)
        except Exception as e:
            self.log(f"search_code error: {e}", "error")
            return []

    def remove_history_item(self, url) -> None:
        try:
            from .core.session_memory import remove_url_from_history_async

            run_sync(remove_url_from_history_async(url))
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
                return {
                    "ip": "Unavailable",
                    "country_name": "Unknown",
                    "country_code": "",
                }

        return run_sync(_inner())

    def save_profile_data(self, name, cfg) -> bool | None:
        try:
            from .core.profiles import save_profile_async

            run_sync(save_profile_async(name, cfg))
            self.log(f"Profile '{name}' saved successfully.", "ok")
            return True
        except Exception as e:
            self.log(f"Failed to save profile: {e}", "error")
            return False

    def delete_profile_data(self, name):
        try:
            from .core.profiles import delete_profile_async

            deleted = run_sync(delete_profile_async(name))
            self.log(
                f"Profile '{name}' deleted: {deleted}.",
                "ok" if deleted else "warn",
            )
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
                    msg = "URL must be a non-empty string"
                    raise ValueError(msg)
                normalized = url.strip()
                if not normalized.lower().startswith("http"):
                    msg = "URL must start with http or https"
                    raise ValueError(msg)
                client = AsyncHttpClient("api-check", timeout_s=10)
                resp = await client.get(normalized, follow_redirects=True, timeout=10.0)
                return {
                    "ok": 200 <= resp.status_code < 400,
                    "status_code": resp.status_code,
                    "url": str(resp.url),
                }
            except Exception as e:
                return {"ok": False, "error": str(e)}

        return run_sync(_inner())

    # ── Window controls ───────────────────────────────────────────────────────

    def WindowMinimise(self) -> None:
        if self._window:
            self._window.minimize()

    def WindowToggleMaximise(self) -> None:
        if not self._window:
            return
        if getattr(self, "_is_maximized", False):
            self._window.restore()
            self._is_maximized = False
        elif sys.platform == "win32":
            try:
                import ctypes

                # Get monitor work area (excluding taskbar)
                work_area = ctypes.wintypes.RECT()
                # SPI_GETWORKAREA = 48
                result = ctypes.windll.user32.SystemParametersInfoW(
                    48,
                    0,
                    ctypes.byref(work_area),
                    0,
                )
                if result:
                    width = work_area.right - work_area.left
                    height = work_area.bottom - work_area.top
                    # Use SetWindowPos API for more reliable positioning
                    hwnd = ctypes.windll.user32.FindWindowW(
                        None,
                        self._window.title,
                    )
                    if hwnd:
                        # SWP_NOZORDER = 4, SWP_FRAMECHANGED = 0x20
                        ctypes.windll.user32.SetWindowPos(
                            hwnd,
                            None,
                            work_area.left,
                            work_area.top,
                            width,
                            height,
                            4 | 0x20,
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

    def Quit(self) -> None:
        if self._window:
            with contextlib.suppress(Exception):
                self._window.destroy()
        os._exit(0)

    def choose_folder(self) -> None:
        """Desktop-only: opens the native OS folder picker. In web mode
        self._window is always None here, so this is a safe no-op — the web
        frontend uses set_download_dir() (below) with its own server-side
        folder browser instead, since a browser cannot open a native dialog
        that returns a real filesystem path on the server.
        """
        if self._window:
            dialog_type = getattr(webview, "FileDialog", None)
            if dialog_type is None:
                dialog_type = getattr(webview, "FOLDER_DIALOG", None)
            if dialog_type is None:
                return
            result = self._window.create_file_dialog(
                dialog_type.FOLDER if hasattr(dialog_type, "FOLDER") else dialog_type
            )
            if result and len(result) > 0:
                self.download_dir = result[0]
                self.log(f"Download folder changed: {self.download_dir}", "ok")
                with contextlib.suppress(Exception):
                    self._push("updateFolderLabel", self.download_dir)

    def get_home_dir(self) -> str:
        """Return the user's home dir for desktop and browser folder pickers."""
        return str(Path.home())

    def browse_folder(self, path: str | None = None) -> dict:
        """List subdirectories and files for a folder, matching the web-mode /api/browse-folder payload."""
        base = Path(path).expanduser() if path else Path.home()
        try:
            base = base.resolve()
            if not base.is_dir():
                base = Path.home().resolve()
            directories = sorted(
                (
                    p.name
                    for p in base.iterdir()
                    if p.is_dir() and not p.name.startswith(".")
                ),
                key=str.lower,
            )
            files = sorted(
                (
                    p.name
                    for p in base.iterdir()
                    if p.is_file() and not p.name.startswith(".")
                ),
                key=str.lower,
            )
        except Exception as e:
            return {
                "error": str(e),
                "path": str(base),
                "parent": None,
                "directories": [],
                "files": [],
            }
        parent = str(base.parent) if base.parent != base else None
        return {
            "path": str(base),
            "parent": parent,
            "directories": directories,
            "files": files,
        }

    def set_download_dir(self, path: str) -> dict:
        """Web-mode equivalent of choose_folder(): sets the download
        directory to a path chosen via the server-side folder browser
        (see webapp.py's /api/browse-folder). Validates that the path
        exists and is a directory on the server before accepting it.
        """
        p = Path(path).expanduser()
        if not p.is_dir():
            return {"ok": False, "error": f"Not a directory: {path}"}
        self.download_dir = str(p)
        self.log(f"Download folder changed: {self.download_dir}", "ok")
        with contextlib.suppress(Exception):
            self._push("updateFolderLabel", self.download_dir)
        return {"ok": True, "download_dir": self.download_dir}

    def open_config_folder(self) -> None:
        config_dir = os.path.join(os.path.expanduser("~"), ".cache", "spotiflac")
        try:
            os.makedirs(config_dir, exist_ok=True)
            if sys.platform == "darwin":
                subprocess.Popen(["open", config_dir])
            elif sys.platform == "win32":
                os.startfile(config_dir)
            else:
                subprocess.Popen(["xdg-open", config_dir])
            self.log(f"Opened config folder: {config_dir}", "ok")
        except Exception as e:
            self.log(f"Failed opening config folder: {e}", "error")

    def open_url(self, url) -> None:
        """Opens an http(s) URL in the user's browser.

        The scheme check is not cosmetic: webbrowser.open() delegates to
        `open` on macOS and `xdg-open` on Linux, both of which resolve every
        scheme the desktop has a handler registered for — file://, and
        whatever custom scheme any installed application claimed. Passing
        those through would turn "open a link" into "launch an arbitrary
        local handler", reachable from any JS running in the window.
        """
        import webbrowser
        from urllib.parse import urlparse

        try:
            scheme = urlparse(str(url)).scheme.lower()
        except Exception:
            scheme = ""
        if scheme not in ("http", "https"):
            self.log(
                f"Refused to open a non-web URL (scheme: {scheme or 'none'})", "error"
            )
            return

        webbrowser.open(url)

    # ── Lazy Loading - Track preview ──────────────────────────────────────

    def get_track_preview(self, track_id: str) -> str:
        """Retrieves the preview URL for a track (lazy loading).

        This method is only invoked by the GUI when the user clicks 'play' or 'preview'
        to avoid network requests during initial list loading.

        Args:
            track_id: Spotify track ID

        Returns:
            MP3 preview URL (empty string if unavailable)

        """
        try:
            from .core.spotify_metadata import SpotifyMetadataClient

            client = SpotifyMetadataClient()
            preview_url = run_sync(client.get_track_preview_async(track_id))
            return preview_url or ""
        except Exception as e:
            self.log(f"Failed to fetch preview for track {track_id}: {e}", "debug")
            return ""

    # ── Phase 1: Metadata fetch ───────────────────────────────────────────────

    def fetch_metadata(self, url) -> None:
        self.current_url = url
        threading.Thread(
            target=lambda: run_sync(self._fetch_metadata_task(url)),
            daemon=True,
        ).start()

    async def _fetch_metadata_task(self, url) -> None:
        try:
            self.set_progress("Retrieving metadata…")
            self.log(f"Analyzing input: {url}", "debug")

            # ── Detection: is it a URL or a search query? ──────────────────
            stripped = url.strip()
            is_url = stripped.startswith(("http", "spotify:"))

            if is_url:
                # ── Scelta client in base al dominio ───────────────────────────
                if url_host_matches(url, "tidal.com"):
                    from .core.tidal_metadata import TidalMetadataClient

                    client = TidalMetadataClient()
                elif url_host_matches(url, "music.apple.com"):
                    from .core.apple_music_metadata import AppleMusicMetadataClient

                    client = AppleMusicMetadataClient()
                else:
                    from .core.spotify_metadata import SpotifyMetadataClient

                    client = SpotifyMetadataClient()
            else:
                # ── Text search — always SpotifyMetadataClient ─────────────
                from .core.spotify_metadata import SpotifyMetadataClient

                client = SpotifyMetadataClient()
            if not is_url:
                self.log("Text search: use search_provider_async.", "error")
                self.set_progress("")
                return

            # ── Universal call ──────────────────────────────────────────────────
            # get_url() is sync on SpotifyMetadataClient but async on
            # TidalMetadataClient/AppleMusicMetadataClient — calling it directly
            # here would hand back an un-awaited coroutine for the latter two
            # (crashing on the very next line, `result[0]`). Reuse the same
            # sync/async dispatch helper the real download path already relies
            # on instead of duplicating (and re-diverging from) that logic.
            # Returns (name, tracks) OR (name, tracks, cover) OR (name, tracks, cover, meta).
            from .downloader import _call_metadata_get_url

            result = await _call_metadata_get_url(client, stripped)
            collection_name = result[0]
            tracks = result[1]
            collection_cover = result[2] if len(result) > 2 else ""
            collection_meta = result[3] if len(result) > 3 else {}

            cover = collection_cover or ""
            lower_url = url.lower()
            is_playlist = ("/playlist/" in lower_url) or (
                "list=" in lower_url and "olak5uy_" not in lower_url
            )
            is_artist = "/artist/" in lower_url or "spotify:artist:" in lower_url

            if not cover and not is_playlist and not is_artist and tracks:
                cover = getattr(tracks[0], "cover_url", "") or ""

            if not tracks:
                self.log("No tracks found at this URL.", "error")
                return

            self.current_tracks = tracks
            track_data = []

            # Retrieve playcount from Spotify if applicable (non-blocking)
            playcount_map = {}
            if url_host_matches(url, "spotify.com"):
                try:
                    from .core.spotfetch import SpotifyWebClient

                    sp_client = SpotifyWebClient()

                    try:
                        # Initialize with timeout (5 seconds)
                        sp_client.initialize()

                        # Try to extract playlist / track / artist info from URL

                        playlist_match = re.search(r"playlist[:/]([a-zA-Z0-9]+)", url)
                        track_match = re.search(r"track[:/]([a-zA-Z0-9]+)", url)
                        album_match = re.search(r"album[:/]([A-Za-z0-9]+)", url)
                        lower_url = url.lower()
                        is_artist = (
                            "/artist/" in lower_url or "spotify:artist:" in lower_url
                        )

                        if playlist_match:
                            self.log("Attempting to fetch playcount…", "debug")
                            playlist_id = playlist_match.group(1)
                            playcount_map = sp_client.get_playlist_stats(playlist_id)
                        elif track_match and len(tracks) == 1 and not is_artist:
                            self.log("Attempting to fetch playcount…", "debug")
                            track_id = track_match.group(1)
                            stats = sp_client.get_track_stats(track_id)
                            if stats.get("playcount"):
                                playcount_map[track_id] = stats.get("playcount")
                        elif album_match:
                            self.log(
                                "Attempting to fetch playcount for album (fast mode)…",
                                "debug",
                            )
                            album_id = album_match.group(1)
                            playcount_map = sp_client.get_album_stats(album_id)
                        elif is_artist:
                            playcount_map = {}
                        else:
                            pass
                    except Exception as auth_err:
                        self.log(
                            f"Playcount unavailable: {type(auth_err).__name__}",
                            "debug",
                        )

                except Exception:
                    pass  # Silently skip playcount on any error

            # ── Enhanced metadata-extraction loop ──
            for i, t in enumerate(tracks):
                track_id = getattr(t, "id", "")

                # Try to retrieve data dynamically
                title = getattr(t, "title", getattr(t, "name", f"Track {i + 1}"))
                # Handles both cases where 'artists' is a string or a list
                raw_art = getattr(t, "artists", getattr(t, "artist", "Unknown"))
                artist = (
                    ", ".join(raw_art) if isinstance(raw_art, list) else str(raw_art)
                )

                # Take the album if it exists
                album = getattr(t, "album", getattr(t, "album_name", "—"))

                _pc_val = playcount_map.get(track_id, "") if playcount_map else ""
                playcount = (
                    _pc_val.get("playcount", "")
                    if isinstance(_pc_val, dict)
                    else _pc_val
                )
                if not playcount or playcount == "0":
                    fallback_plays = getattr(t, "plays", "")
                    playcount = (
                        fallback_plays
                        if fallback_plays and fallback_plays != "0"
                        else ""
                    )

                track_data.append(
                    {
                        "index": i,
                        "id": track_id,
                        "title": title,
                        "artist": artist,
                        "album": album,
                        "cover": getattr(t, "cover_url", ""),
                        "duration_ms": getattr(t, "duration_ms", 0),
                        "explicit": getattr(t, "is_explicit", False),
                        "isrc": getattr(t, "isrc", ""),
                        "external_url": getattr(t, "external_url", ""),
                        "preview_url": getattr(t, "preview_url", ""),
                        "playcount": playcount,
                        "release_date": getattr(t, "release_date", ""),
                        "copyright": getattr(t, "copyright", ""),
                    },
                )

            badge = f"FLAC — {len(tracks)} tracks" if len(tracks) > 1 else "FLAC"

            # For artist URLs show only artist name
            lower_url = url.lower()
            is_artist = "/artist/" in lower_url or "spotify:artist:" in lower_url
            if is_artist:
                display_title = collection_name
                display_artist = ""
            else:
                display_title = collection_name
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

            self.log(
                f"Found: {collection_name} ({len(tracks)} track(s)). Choose songs to download.",
                "ok",
            )
            self.set_progress("Ready for download.")

            try:
                from .core.session_memory import add_url_to_history_async

                _lower = url.lower()
                if (
                    "/track/" in _lower
                    or _lower.startswith("spotify:track:")
                    or "watch?v=" in _lower
                    or "youtu.be" in _lower
                ):
                    _url_type = "track"
                elif "/album/" in _lower or _lower.startswith("spotify:album:"):
                    _url_type = "album"
                elif (
                    "/playlist/" in _lower
                    or _lower.startswith("spotify:playlist:")
                    or ("list=" in _lower and "olak5uy_" not in _lower)
                ):
                    _url_type = "playlist"
                elif "/artist/" in _lower or _lower.startswith("spotify:artist:"):
                    _url_type = "artist"
                else:
                    _url_type = ""

                _artist = (
                    getattr(tracks[0], "artists", "")
                    if tracks and _url_type == "track"
                    else ""
                )
                await add_url_to_history_async(
                    url,
                    label=collection_name,
                    cover=cover,
                    track_count=len(tracks),
                    url_type=_url_type,
                    artist=_artist,
                )
            except Exception:
                pass

            try:
                self._push("showTracklist", track_data)
            except Exception:
                pass

        except Exception as e:
            self.log(f"Error fetching metadata: {e!s}", "error")
            self.set_progress("Error.")

    # ── Phase 2: Download ─────────────────────────────────────────────────────

    def download_tracks(self, selected_indices, config) -> None:
        threading.Thread(
            target=self._download_task,
            args=(selected_indices, config),
            daemon=True,
        ).start()

    def _download_task(self, selected_indices, config) -> None:
        self._download_active.set()
        sf_logger = logging.getLogger("SpotiFLAC")
        handler = UILogHandler(self)
        handler.setFormatter(logging.Formatter("[%(name)s] %(message)s"))
        sf_logger.addHandler(handler)
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(logging.Formatter("  %(message)s"))
        sf_logger.addHandler(console_handler)
        monitor_stop = None
        monitor_thread = None

        log_level_str = config.get("log_level", "INFO")
        current_log_level = logging.DEBUG if log_level_str == "DEBUG" else logging.INFO
        sf_logger.setLevel(current_log_level)

        try:
            os.makedirs(self.download_dir, exist_ok=True)

            quality = config.get("quality", "LOSSLESS")
            allow_fallback = config.get("allow_fallback", True)
            embed_lyrics = config.get("lyrics", True)
            enrich_metadata = config.get("enrich_metadata", True)
            services = config.get("services", ["tidal", "qobuz", "deezer"])
            filename_format = config.get("filename_format", "{title} - {artist}")
            use_track_numbers = config.get("use_track_numbers", False)
            use_album_track_numbers = config.get("use_album_track_numbers", False)
            use_artist_subfolders = config.get("use_artist_subfolders", False)
            use_album_subfolders = config.get("use_album_subfolders", False)
            first_artist_only = config.get("first_artist_only", False)
            artist_separator = config.get("artist_separator") or None
            lyrics_providers = config.get("lyrics_providers") or [
                "apple",
                "lrclib",
            ]
            enrich_providers = config.get("enrich_providers") or [
                "deezer",
                "apple",
                "qobuz",
                "tidal",
                "soundcloud",
            ]
            from .core.transcode import normalize_transcode_format

            # The GUI sends "none" when conversion is disabled
            transcode_to = normalize_transcode_format(config.get("transcode_to"))
            transcode_bitrate = config.get("transcode_bitrate") or "320k"
            transcode_keep_original = config.get("transcode_keep_original", False)
            track_max_retries = int(config.get("track_max_retries", 0))
            post_download_action = config.get("post_download_action", "none")
            post_download_command = config.get("post_download_command", "")
            if post_download_action == "command" and not _post_command_allowed():
                # See _post_command_allowed(): "open_folder" and "notify" stay
                # available to the GUI unconditionally — only the shell action
                # needs the operator to have opted in when starting the process.
                self.log(
                    "Post-download command ignored: running a shell command from "
                    f"the interface is disabled. Set {POST_COMMAND_ENV}=1 before "
                    "starting SpotiFLAC to enable it.",
                    "error",
                )
                post_download_action = "none"
                post_download_command = ""
            qobuz_local_api_url = config.get("qobuz_local_api_url") or None
            tidal_custom_api = config.get("tidal_custom_api") or None
            loop_val = config.get("loop", None)
            loop_minutes = int(loop_val) if loop_val else None

            if not services:
                self.log("Error: select at least one service.", "error")
                return

            collection_url = (self.current_url or "").strip()
            # A track list loaded from a CSV has no collection URL to stand
            # for it (see api_mixins/csv_import.py), so the whole-collection
            # shortcut only applies when there really is one.
            if collection_url.startswith(("http", "spotify:")) and len(
                selected_indices
            ) == len(self.current_tracks):
                urls_to_download = [collection_url]
                self.log("Downloading entire album/playlist…", "debug")
            else:
                urls_to_download = []
                unresolved: list[str] = []
                for i in selected_indices:
                    t = self.current_tracks[i]
                    t_url = getattr(t, "external_url", None) or getattr(t, "url", None)
                    t_id = getattr(t, "id", None)
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
                        # Quiet: a tracklist that carries no links at all —
                        # a CSV of bare titles, say — hits this for every
                        # selected row, and one toast per row would be a
                        # wall of them. Counted into one toast below.
                        unresolved.append(t.title)
                        self.log(
                            f"Could not resolve URL for '{t.title}'. Skipping.",
                            "error-quiet",
                        )
                if unresolved:
                    self.log(
                        f"Skipped {len(unresolved)} track(s) with no resolvable "
                        "link — see the Logs view for which.",
                        "warn",
                    )

            if not urls_to_download:
                self.log("No valid URLs to download.", "error")
                return

            if transcode_to:
                self.log(
                    f"Transcoding enabled — tracks will be saved as "
                    f"{transcode_to.upper()} {transcode_bitrate}"
                    + ("" if transcode_keep_original else " (originals removed)"),
                    "debug",
                )

            self.set_progress(f"Downloading ({quality})…")
            monitor_stop = threading.Event()
            monitor_thread = threading.Thread(
                target=self._download_stats_monitor,
                args=(monitor_stop,),
                daemon=True,
            )
            monitor_thread.start()

            from . import SpotiFLAC
            from .core.download_log import record_hook

            # The same hook the CLI installs (see launcher._run_download_async).
            # Without it nothing a GUI or `--web` user downloaded was ever
            # written down: per-account quotas had nothing to count, and the
            # dashboard would have shown an empty history to precisely the
            # people who never touch the CLI.
            log_hook = record_hook(self.owner)

            for u in urls_to_download:
                SpotiFLAC(
                    url=u,
                    output_dir=self.download_dir,
                    services=services,
                    quality=quality,
                    allow_fallback=allow_fallback,
                    filename_format=filename_format,
                    use_track_numbers=use_track_numbers,
                    use_album_track_numbers=use_album_track_numbers,
                    use_artist_subfolders=use_artist_subfolders,
                    use_album_subfolders=use_album_subfolders,
                    first_artist_only=first_artist_only,
                    artist_separator=artist_separator,
                    embed_lyrics=embed_lyrics,
                    lyrics_providers=lyrics_providers,
                    enrich_metadata=enrich_metadata,
                    enrich_providers=enrich_providers,
                    qobuz_local_api_url=qobuz_local_api_url,
                    tidal_custom_api=tidal_custom_api,
                    transcode_to=transcode_to,
                    transcode_bitrate=transcode_bitrate,
                    transcode_keep_original=transcode_keep_original,
                    track_max_retries=track_max_retries,
                    post_download_action=post_download_action,
                    post_download_command=post_download_command,
                    log_level=current_log_level,
                    loop=loop_minutes,
                    post_download_hooks=[log_hook],
                )

            self._push_download_stats()
            self.set_progress("Complete!")
            self.log(f"All tracks saved to: {self.download_dir}", "ok")
            try:
                self._push("app_download_finished", True)
            except Exception:
                pass

        except Exception as e:
            self.log(f"Download error: {e!s}", "error")
            self.set_progress("Error.")
            self._push_download_stats()
            try:
                self._push("app_download_finished", False)
            except Exception:
                pass
        finally:
            if monitor_stop is not None:
                monitor_stop.set()
            if monitor_thread is not None:
                monitor_thread.join(timeout=1)
            self._push_download_stats()
            sf_logger.removeHandler(handler)
            if "console_handler" in locals():
                sf_logger.removeHandler(console_handler)
            self._download_active.clear()

    # ── Health Check ──────────────────────────────────────────────────────────

    def run_health_check(self, services) -> None:
        threading.Thread(
            target=self._health_check_task,
            args=(services,),
            daemon=True,
        ).start()

    def _download_stats_monitor(self, stop_event) -> None:
        try:
            from .core.progress import DownloadManager

            manager = DownloadManager()
            while not stop_event.wait(0.25):
                self._push_download_stats(manager.get_stats_sync())
        except Exception:
            pass
        finally:
            self._push_download_stats()

    def _push_download_stats(self, stats=None) -> None:
        try:
            if stats is None:
                from .core.progress import DownloadManager

                stats = DownloadManager().get_stats_sync()
            self._push("app_update_download_stats", stats)
        except Exception:
            pass

    def _health_check_task(self, services) -> None:
        """Run lyrics provider health checks and update the frontend."""
        try:
            from .core.health_check import run_health_check

            self.log(f"Health check started for: {', '.join(services)}", "debug")
            results = run_sync(run_health_check(services))
            data = [
                {
                    "provider": r.provider,
                    "method": r.method,
                    "url": r.url,
                    "ok": r.ok,
                    "latency": round(r.latency) if r.latency >= 0 else -1,
                    "detail": r.detail,
                }
                for r in results
            ]
            ok_providers = [r.provider for r in results if r.ok]
            self.log(
                f"Health check — {len([r for r in results if r.ok])}/{len(results)} endpoints OK.",
                "ok" if ok_providers else "error",
            )
            try:
                self._push("updateHealthResults", data)
            except Exception:
                pass
        except ImportError:
            self.log("health_check module not found.", "error")
        except Exception as e:
            self.log(f"Health check error: {e!s}", "error")


#: Port the desktop window's bundled HTTP server prefers, so the page keeps
#: one origin (and therefore one localStorage) across launches. See the
#: comment at webview.start() below for why that matters.
GUI_HTTP_PORT = 47251


def _pick_gui_port() -> int | None:
    """`GUI_HTTP_PORT` when it is free, otherwise None (pick any port).

    Falling back rather than failing is deliberate: a busy port costs a
    stable origin — the window flashes light for a frame before app.js
    applies the stored theme — while refusing to start costs the whole
    application. The theme itself survives either way, because it is
    stored server-side in gui-settings.json.
    """
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", GUI_HTTP_PORT))
    except OSError:
        return None
    return GUI_HTTP_PORT


def run_gui() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    logging.getLogger("pywebview").setLevel(logging.WARNING)
    api = SpotiFLAC_API()

    # Try several candidate locations for the frontend files to be robust
    candidates = []

    # 1) frontend next to the installed module file (site-packages/frontend)
    site_packages_frontend = os.path.join(
        os.path.dirname(__file__),
        "frontend",
        "index.html",
    )
    candidates.append(site_packages_frontend)

    # 2) frontend inside the backend package (if present)
    try:
        import SpotiFLAC as _sp_pkg

        pkg_frontend = os.path.join(
            os.path.dirname(_sp_pkg.__file__),
            "frontend",
            "index.html",
        )
        candidates.append(pkg_frontend)
    except Exception:
        pass

    # 3) original heuristic (parent of site-packages, e.g. lib/pythonX.Y/frontend)
    original_frontend = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "frontend", "index.html"),
    )
    candidates.append(original_frontend)

    # 4) installation data dir (some installers place data files under the install prefix)
    try:
        import sysconfig

        data_dir = sysconfig.get_paths().get("data")
        if data_dir:
            candidates.append(os.path.join(data_dir, "frontend", "index.html"))
    except Exception:
        pass

    # Pick the first existing candidate
    html_path = None
    for p in candidates:
        if os.path.exists(p):
            html_path = p
            break

    if not html_path:
        msg = f"index.html not found. Tried: {candidates}"
        raise FileNotFoundError(msg)

    window = webview.create_window(
        "SpotiFLAC",
        url=html_path,
        js_api=api,
        width=1300,
        height=850,
        min_size=(1000, 800),
        frameless=True,
        easy_drag=False,
        background_color="#0a0a0a",
    )
    api.set_window(window)
    window.events.loaded += api._on_loaded

    # Two defaults conspired to make the theme picker look broken in the
    # desktop window, both of them about *where* the page's localStorage
    # lives:
    #
    #   private_mode=True (pywebview's default) throws the web view's
    #   storage away when the process exits, so 'spotiflac-theme-mode' was
    #   never there on the next launch.
    #
    #   http_server=True with no port serves index.html from a *random*
    #   free port, and http://127.0.0.1:51234 and http://127.0.0.1:51235 are
    #   different origins with different localStorage. So even within a
    #   single session's lifetime the storage was thrown away again on the
    #   next launch, private mode or not.
    #
    # The result was that picking Dark applied instantly and then came back
    # light on every restart. A fixed port plus a real storage directory
    # gives the page one stable origin whose contents survive, which is what
    # the pre-paint script in index.html reads. The theme no longer depends
    # on it either (save_theme() writes it to gui-settings.json — see
    # changeTheme() in frontend/app.js), but a stable origin is what keeps
    # the window from painting light for a frame first.
    storage_path = str(Path.home() / ".cache" / "spotiflac" / "webview")
    with contextlib.suppress(Exception):
        Path(storage_path).mkdir(parents=True, exist_ok=True)

    webview.start(
        http_server=True,
        http_port=_pick_gui_port(),
        private_mode=False,
        storage_path=storage_path,
    )


if __name__ == "__main__":
    run_gui()
