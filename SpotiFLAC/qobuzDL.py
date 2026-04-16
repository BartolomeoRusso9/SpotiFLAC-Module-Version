import hashlib
import json
import os
import re
import random
import threading
import time
from typing import Callable, Dict, Optional, Tuple

import requests
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType

# ---------------------------------------------------------------------------
# Costanti
# ---------------------------------------------------------------------------

QOBUZ_API_BASE_URL              = "https://www.qobuz.com/api.json/0.2"
QOBUZ_DEFAULT_APP_ID            = "712109809"
QOBUZ_DEFAULT_APP_SECRET        = "589be88e4538daea11f509d29e4a23b1"
QOBUZ_DEFAULT_UA                = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
QOBUZ_CREDENTIALS_CACHE_TTL     = 24 * 3600          # seconds
QOBUZ_CREDENTIALS_PROBE_ISRC   = "USUM71703861"
QOBUZ_OPEN_TRACK_PROBE_URL     = "https://open.qobuz.com/track/1"
QOBUZ_CREDENTIALS_CACHE_FILE   = "qobuz-api-credentials.json"

_BUNDLE_SCRIPT_RE = re.compile(
    r'<script[^>]+src="([^"]+/js/main\.js|/resources/[^"]+/js/main\.js)"'
)
_API_CONFIG_RE = re.compile(
    r'app_id:"(?P<app_id>\d{9})",app_secret:"(?P<app_secret>[a-f0-9]{32})"'
)

# ---------------------------------------------------------------------------
# Utility filename
# ---------------------------------------------------------------------------

def _sanitize_filename(value: str, fallback: str = "Unknown") -> str:
    if not value:
        return fallback
    cleaned = re.sub(r'[\\/*?:"<>|]', "", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or fallback

def get_first_artist(artist_str: str) -> str:
    if not artist_str:
        return "Unknown"
    return artist_str.split(",")[0].strip()

def build_qobuz_filename(title, artist, album, album_artist, release_date,
                         track_number, disc_number, format_string,
                         include_track_number, position, use_album_track_number):
    number_to_use = track_number if use_album_track_number and track_number > 0 else position
    year = release_date[:4] if len(release_date) >= 4 else ""

    if "{" in format_string:
        filename = (format_string
                    .replace("{title}", title)
                    .replace("{artist}", artist)
                    .replace("{album}", album)
                    .replace("{album_artist}", album_artist)
                    .replace("{year}", year)
                    .replace("{date}", _sanitize_filename(release_date)))
        if disc_number > 0:
            filename = filename.replace("{disc}", str(disc_number))
        else:
            filename = filename.replace("{disc}", "")
        if number_to_use > 0:
            filename = filename.replace("{track}", f"{number_to_use:02d}")
        else:
            filename = re.sub(r"\{track\}[\.\s-]*", "", filename)
    else:
        if format_string == "artist-title":
            filename = f"{artist} - {title}"
        elif format_string == "title":
            filename = title
        else:
            filename = f"{title} - {artist}"
        if include_track_number and position > 0:
            filename = f"{number_to_use:02d}. {filename}"

    return _sanitize_filename(filename) + ".flac"

def build_qobuz_api_url(api_base: str, track_id: int, quality: str) -> str:
    if "qbz.afkarxyz.qzz.io" in api_base:
        return f"{api_base}{track_id}?quality={quality}"
    return f"{api_base}{track_id}&quality={quality}"

# ---------------------------------------------------------------------------

class QobuzCredentials:
    def __init__(self, app_id: str, app_secret: str,
                 source: str = "", fetched_at: Optional[float] = None):
        self.app_id     = app_id
        self.app_secret = app_secret
        self.source     = source
        self.fetched_at = fetched_at or time.time()

    def is_fresh(self) -> bool:
        return (
            bool(self.app_id) and bool(self.app_secret) and
            (time.time() - self.fetched_at) < QOBUZ_CREDENTIALS_CACHE_TTL
        )

    def to_dict(self) -> dict:
        return {
            "app_id":         self.app_id,
            "app_secret":     self.app_secret,
            "source":         self.source,
            "fetched_at_unix": int(self.fetched_at),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "QobuzCredentials":
        return cls(
            app_id     = d.get("app_id", ""),
            app_secret = d.get("app_secret", ""),
            source     = d.get("source", ""),
            fetched_at = float(d.get("fetched_at_unix", 0)),
        )

    @classmethod
    def default(cls) -> "QobuzCredentials":
        return cls(QOBUZ_DEFAULT_APP_ID, QOBUZ_DEFAULT_APP_SECRET,
                   source="embedded-default")


def _credentials_cache_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".cache",
                        "spotiflac", QOBUZ_CREDENTIALS_CACHE_FILE)

def _load_cached_credentials() -> Optional[QobuzCredentials]:
    path = _credentials_cache_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            return QobuzCredentials.from_dict(json.load(f))
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"Warning: failed to read Qobuz credentials cache: {e}")
        return None

def _save_cached_credentials(creds: QobuzCredentials) -> None:
    path = _credentials_cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(creds.to_dict(), f, indent=2)
    except Exception as e:
        print(f"Warning: failed to write Qobuz credentials cache: {e}")

def _scrape_open_qobuz_credentials(session: requests.Session) -> QobuzCredentials:
    headers = {"User-Agent": QOBUZ_DEFAULT_UA}

    resp = session.get(QOBUZ_OPEN_TRACK_PROBE_URL, headers=headers, timeout=15)
    resp.raise_for_status()

    m = _BUNDLE_SCRIPT_RE.search(resp.text)
    if not m:
        raise RuntimeError("Qobuz open bundle URL not found in HTML")

    bundle_url = m.group(1)
    if bundle_url.startswith("/"):
        bundle_url = "https://open.qobuz.com" + bundle_url

    bundle_resp = session.get(bundle_url, headers=headers, timeout=30)
    bundle_resp.raise_for_status()

    cm = _API_CONFIG_RE.search(bundle_resp.text)
    if not cm:
        raise RuntimeError("app_id/app_secret pair not found in Qobuz bundle")

    return QobuzCredentials(
        app_id     = cm.group("app_id"),
        app_secret = cm.group("app_secret"),
        source     = bundle_url,
    )

def _probe_credentials(session: requests.Session, creds: QobuzCredentials) -> bool:
    try:
        resp = _do_signed_request(
            "track/search",
            {"query": QOBUZ_CREDENTIALS_PROBE_ISRC, "limit": "1"},
            session, creds
        )
        data = resp.json()
        return data.get("tracks", {}).get("total", 0) > 0
    except Exception:
        return False

_creds_lock             = threading.Lock()
_cached_creds: Optional[QobuzCredentials] = None

def _get_credentials(force_refresh: bool = False) -> QobuzCredentials:
    global _cached_creds

    with _creds_lock:
        if not force_refresh and _cached_creds and _cached_creds.is_fresh():
            return _cached_creds

        disk = _load_cached_credentials()
        if not force_refresh and disk and disk.is_fresh():
            _cached_creds = disk
            return _cached_creds

        try:
            session = requests.Session()
            scraped = _scrape_open_qobuz_credentials(session)
            if _probe_credentials(session, scraped):
                _cached_creds = scraped
                _save_cached_credentials(scraped)
                print(f"Loaded fresh Qobuz credentials from {scraped.source} (app_id={scraped.app_id})")
                return _cached_creds
            raise RuntimeError("scraped credentials did not pass validation")
        except Exception as e:
            print(f"Warning: failed to refresh Qobuz credentials: {e}")

        if disk:
            _cached_creds = disk
            print("Warning: using stale cached Qobuz credentials")
            return _cached_creds

        if _cached_creds:
            return _cached_creds

        fallback = QobuzCredentials.default()
        _cached_creds = fallback
        print("Warning: using embedded fallback Qobuz credentials")
        return fallback

# ---------------------------------------------------------------------------

def _build_signature_payload(path: str, params: dict,
                              timestamp: str, secret: str) -> str:

    normalized = path.strip("/").replace("/", "")
    excluded   = {"app_id", "request_ts", "request_sig"}
    sorted_keys = sorted(k for k in params if k not in excluded)

    payload = normalized
    for key in sorted_keys:
        val = params[key]
        if isinstance(val, list):
            for v in val:
                payload += key + str(v)
        else:
            payload += key + str(val)

    payload += timestamp + secret
    return payload

def _compute_signature(path: str, params: dict,
                        timestamp: str, secret: str) -> str:
    payload = _build_signature_payload(path, params, timestamp, secret)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()

def _do_signed_request(path: str, params: dict,
                        session: requests.Session,
                        creds: QobuzCredentials) -> requests.Response:
    timestamp = str(int(time.time()))
    signature = _compute_signature(path, params, timestamp, creds.app_secret)

    req_params = dict(params)
    req_params["app_id"]      = creds.app_id
    req_params["request_ts"]  = timestamp
    req_params["request_sig"] = signature

    url = f"{QOBUZ_API_BASE_URL}/{path.strip('/')}"
    headers = {
        "User-Agent": QOBUZ_DEFAULT_UA,
        "Accept":     "application/json",
        "X-App-Id":   creds.app_id,
    }
    return session.get(url, params=req_params, headers=headers, timeout=20)

# ---------------------------------------------------------------------------

class QobuzDownloader:
    def __init__(self, timeout: float = 60.0):
        self.timeout          = timeout
        self.session          = requests.Session()
        self.session.timeout  = timeout
        self.progress_callback: Callable[[int, int], None] = lambda c, t: None

    def set_progress_callback(self, callback: Callable[[int, int], None]) -> None:
        self.progress_callback = callback

    # ------------------------------------------------------------------

    def _signed_request(self, path: str, params: dict) -> requests.Response:

        creds = _get_credentials(force_refresh=False)
        resp  = _do_signed_request(path, params, self.session, creds)

        if resp.status_code in (400, 401):
            resp.close()
            creds = _get_credentials(force_refresh=True)
            resp  = _do_signed_request(path, params, self.session, creds)

        return resp

    # ------------------------------------------------------------------

    def _search_by_isrc(self, isrc: str) -> Dict:
        if isrc.startswith("qobuz_"):
            track_id = isrc.removeprefix("qobuz_")
            resp = self._signed_request("track/get", {"track_id": track_id})

            if resp.status_code != 200:
                body_preview = resp.text[:200] + ("..." if len(resp.text) > 200 else "")
                try:
                    err_msg = resp.json().get("message", f"Status {resp.status_code}")
                except Exception:
                    err_msg = f"Status {resp.status_code} (Raw: {body_preview})"
                raise Exception(f"API Error: {err_msg}")

            return resp.json()

        # Standard search for ISRC
        resp = self._signed_request("track/search", {"query": isrc, "limit": "1"})

        if resp.status_code != 200:
            body_preview = resp.text[:200] + ("..." if len(resp.text) > 200 else "")
            try:
                err_msg = resp.json().get("message", f"Status {resp.status_code}")
            except Exception:
                err_msg = f"Status {resp.status_code} (Raw: {body_preview})"
            raise Exception(f"API Error: {err_msg}")

        body = resp.text
        if not body.strip():
            raise Exception("API returned empty response")

        try:
            data = resp.json()
        except Exception as e:
            preview = body[:200] + ("..." if len(body) > 200 else "")
            raise Exception(f"Failed to decode response: {e} (response: {preview})")

        items = data.get("tracks", {}).get("items", [])
        if not items:
            raise Exception(f"track not found for ISRC: {isrc}")

        return items[0]

    # ------------------------------------------------------------------
    # Download URL from other providers
    # ------------------------------------------------------------------

    def _download_from_standard(self, api_base: str, track_id: int,
                                  quality: str) -> str:
        url = build_qobuz_api_url(api_base, track_id, quality)
        headers = {"User-Agent": QOBUZ_DEFAULT_UA}
        resp = self.session.get(url, headers=headers, timeout=self.timeout)

        if resp.status_code != 200:
            raise Exception(f"status {resp.status_code}")
        if not resp.text.strip():
            raise Exception("empty body")

        try:
            data = resp.json()
        except Exception:
            raise Exception("invalid response")

        if isinstance(data, dict):
            if data.get("url"):
                return data["url"]
            if data.get("data", {}).get("url"):
                return data["data"]["url"]

        raise Exception("invalid response")

    def get_download_url(self, track_id: int, quality: str,
                          allow_fallback: bool) -> str:
        quality_code = quality if quality not in ("", "5") else "6"
        print(f"Getting download URL for track ID: {track_id} with requested quality: {quality_code}")

        standard_apis = [
            "https://dab.yeet.su/api/stream?trackId=",
            "https://dabmusic.xyz/api/stream?trackId=",
            "https://qbz.afkarxyz.qzz.io/api/track/",
        ]

        def attempt_download(qual: str) -> str:
            providers = [
                {"name": f"Standard({api})",
                 "func": lambda a=api: self._download_from_standard(a, track_id, qual)}
                for api in standard_apis
            ]
            random.shuffle(providers)

            last_err: Optional[Exception] = None
            for p in providers:
                print(f"Trying Provider: {p['name']} (Quality: {qual})...")
                try:
                    url = p["func"]()
                    if url:
                        print("✓ Success")
                        return url
                except Exception as e:
                    print(f"Provider failed: {e}")
                    last_err = e

            raise Exception(last_err)

        last_err: Optional[Exception] = None
        try:
            return attempt_download(quality_code)
        except Exception as e:
            last_err = e

        if allow_fallback:
            current = quality_code

            if current == "27":
                print("⚠ Quality 27 failed, trying fallback to 7 (24-bit Standard)...")
                try:
                    return attempt_download("7")
                except Exception:
                    pass
                current = "7"

            if current == "7":
                print("⚠ Quality 7 failed, trying fallback to 6 (16-bit Lossless)...")
                try:
                    return attempt_download("6")
                except Exception:
                    pass

        raise Exception(f"all APIs and fallbacks failed. Last error: {last_err}")

    # ------------------------------------------------------------------
    # Download FLAC file with progress
    # ------------------------------------------------------------------

    def _stream_download(self, url: str, filepath: str) -> None:
        print("Starting file download...")
        os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
        temp_path = filepath + ".part"

        try:
            with self.session.get(url, stream=True, timeout=300) as resp:
                if resp.status_code != 200:
                    raise Exception(f"download failed with status {resp.status_code}")

                print(f"Creating file: {filepath}")
                print("Downloading...")

                total      = int(resp.headers.get("Content-Length") or 0)
                downloaded = 0
                with open(temp_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=256 * 1024):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            self.progress_callback(downloaded, total)

            os.replace(temp_path, filepath)
            print(f"\rDownloaded: {downloaded / (1024 * 1024):.2f} MB (Complete)")
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass

    # ------------------------------------------------------------------

    def download_by_isrc(self, isrc, output_dir, quality, filename_format,
                          include_track_number, position,
                          spotify_track_name, spotify_artist_name,
                          spotify_album_name, use_album_track_number, **kwargs):

        spotify_album_artist  = kwargs.get("spotify_album_artist",  "Unknown")
        spotify_release_date  = kwargs.get("spotify_release_date",  "")
        spotify_track_number  = kwargs.get("spotify_track_number",  0)
        spotify_disc_number   = kwargs.get("spotify_disc_number",   1)
        spotify_total_tracks  = kwargs.get("spotify_total_tracks",  0)
        spotify_total_discs   = kwargs.get("spotify_total_discs",   1)
        spotify_cover_url     = kwargs.get("spotify_cover_url",     "")
        spotify_copyright     = kwargs.get("spotify_copyright",     "")
        spotify_publisher     = kwargs.get("spotify_publisher",     "")
        spotify_url           = kwargs.get("spotify_url",           "")
        allow_fallback        = kwargs.get("allow_fallback",        True)
        use_first_artist_only = kwargs.get("use_first_artist_only", False)

        print(f"Fetching track info for ISRC: {isrc}")
        if output_dir != ".":
            os.makedirs(output_dir, exist_ok=True)

        track = self._search_by_isrc(isrc)

        q_track_num    = track.get("track_number", 0)
        final_track_num = q_track_num if (use_album_track_number and q_track_num > 0) else position
        if final_track_num == 0 and spotify_track_number > 0:
            final_track_num = spotify_track_number

        print(f"Found track: {spotify_artist_name} - {spotify_track_name}")
        print(f"Album: {spotify_album_name}")

        if track.get("hires", False):
            print(f"Quality: Hi-Res ({track.get('maximum_bit_depth', 24)}-bit / "
                  f"{track.get('maximum_sampling_rate', 96.0)} kHz)")
        else:
            print("Quality: Standard")

        artist_to_use       = (get_first_artist(spotify_artist_name)
                               if use_first_artist_only else spotify_artist_name)
        album_artist_to_use = (get_first_artist(spotify_album_artist)
                               if use_first_artist_only else spotify_album_artist)

        filename = build_qobuz_filename(
            _sanitize_filename(spotify_track_name),
            _sanitize_filename(artist_to_use),
            _sanitize_filename(spotify_album_name),
            _sanitize_filename(album_artist_to_use),
            spotify_release_date, final_track_num, spotify_disc_number,
            filename_format, include_track_number, position, use_album_track_number,
        )
        filepath = os.path.join(output_dir, filename)

        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            size_mb = os.path.getsize(filepath) / (1024 * 1024)
            print(f"File already exists: {filepath} ({size_mb:.2f} MB)")
            return filepath

        print("Getting download URL...")
        download_url = self.get_download_url(track["id"], quality, allow_fallback)

        url_preview = download_url[:60] + ("..." if len(download_url) > 60 else "")
        print(f"Download URL obtained: {url_preview}")

        print(f"Downloading FLAC file to: {filepath}")
        self._stream_download(download_url, filepath)
        print(f"Downloaded: {filepath}")

        cover_path = ""
        if spotify_cover_url:
            cover_path = filepath + ".cover.jpg"
            try:
                with open(cover_path, "wb") as f:
                    f.write(self.session.get(spotify_cover_url, timeout=15).content)
                print("Spotify cover downloaded")
            except Exception as e:
                print(f"Warning: Failed to download Spotify cover: {e}")
                cover_path = ""

        print("Embedding metadata and cover art...")
        metadata = {
            "TITLE":        spotify_track_name,
            "ARTIST":       spotify_artist_name,
            "ALBUM":        spotify_album_name,
            "ALBUMARTIST":  spotify_album_artist,
            "DATE":         spotify_release_date[:4] if len(spotify_release_date) >= 4 else "",
            "TRACKNUMBER":  str(final_track_num),
            "TRACKTOTAL":   str(spotify_total_tracks),
            "DISCNUMBER":   str(spotify_disc_number),
            "DISCTOTAL":    str(spotify_total_discs),
            "ISRC":         isrc,
            "COPYRIGHT":    spotify_copyright,
            "ORGANIZATION": spotify_publisher,
            "URL":          spotify_url,
            "DESCRIPTION":  "https://github.com/ShuShuzinhuu/SpotiFLAC-Module-Version",
        }

        self._embed_metadata(filepath, metadata, cover_path)

        if cover_path and os.path.exists(cover_path):
            try:
                os.remove(cover_path)
            except Exception:
                pass

        return filepath

    # ------------------------------------------------------------------

    def _embed_metadata(self, filepath: str, metadata: dict,
                         cover_path: str) -> None:
        try:
            audio = FLAC(filepath)
            audio.delete()

            for key, val in metadata.items():
                if val and str(val) != "0":
                    audio[key] = str(val)

            if cover_path and os.path.exists(cover_path):
                with open(cover_path, "rb") as img:
                    pic          = Picture()
                    pic.data     = img.read()
                    pic.type     = PictureType.COVER_FRONT
                    pic.mime     = "image/jpeg"
                    audio.add_picture(pic)

            audio.save()
            print("Metadata embedded successfully!")
        except Exception as e:
            raise Exception(f"failed to embed metadata: {e}")