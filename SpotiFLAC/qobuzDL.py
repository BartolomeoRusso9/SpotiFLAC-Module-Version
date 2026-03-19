import json
import os
import re
import random
import time
from typing import Callable, Dict

import requests
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType

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

def build_qobuz_filename(title, artist, album, album_artist, release_date, track_number, disc_number, format_string, include_track_number, position, use_album_track_number):
    number_to_use = track_number if use_album_track_number and track_number > 0 else position
    year = release_date[:4] if len(release_date) >= 4 else ""

    if "{" in format_string:
        filename = (format_string.replace("{title}", title)
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
    if "qbz.afkarxyz.fun" in api_base:
        return f"{api_base}{track_id}?quality={quality}"
    return f"{api_base}{track_id}&quality={quality}"

class QobuzDownloader:
    def __init__(self, timeout: float = 60.0, app_id: str = "798273057"):
        self.timeout = timeout
        self.app_id = app_id
        self.session = requests.Session()
        self.session.timeout = timeout
        self.progress_callback = lambda current, total: None

    def set_progress_callback(self, callback: Callable[[int, int], None]) -> None:
        self.progress_callback = callback

    def _search_by_isrc(self, isrc: str) -> Dict:
        api_base = "https://www.qobuz.com/api.json/0.2/track/search?query="
        url = f"{api_base}{isrc}&limit=1&app_id={self.app_id}"
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
        }
        
        resp = self.session.get(url, headers=headers)
        if resp.status_code != 200:
             try:
                 err_msg = resp.json().get("message", f"Status {resp.status_code}")
             except:
                 err_msg = f"Status {resp.status_code}"
             raise Exception(f"API Error: {err_msg}")

        data = resp.json()
        items = data.get("tracks", {}).get("items", [])
        if not items:
            raise Exception(f"track not found for ISRC: {isrc}")
        return items[0]

    def _download_from_standard(self, api_base: str, track_id: int, quality: str) -> str:
        url = build_qobuz_api_url(api_base, track_id, quality)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
        }
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

    def get_download_url(self, track_id: int, quality: str, allow_fallback: bool) -> str:
        quality_code = quality if quality not in ["", "5"] else "6"
        
        print(f"Getting download URL for track ID: {track_id} with requested quality: {quality_code}")

        standard_apis = [
            "https://dab.yeet.su/api/stream?trackId=",
            "https://dabmusic.xyz/api/stream?trackId=",
            "https://qbz.afkarxyz.fun/api/track/"
        ]

        def attempt_download(qual):
            providers = []
            for api in standard_apis:
                providers.append({"name": f"Standard({api})", "func": lambda a=api: self._download_from_standard(a, track_id, qual)})
            
            random.seed(time.time())
            random.shuffle(providers)
            
            last_err = None
            for p in providers:
                print(f"Trying Provider: {p['name']} (Quality: {qual})...")
                try:
                    url = p['func']()
                    if url:
                        print("✓ Success")
                        return url
                except Exception as e:
                    print(f"Provider failed: {e}")
                    last_err = e
                    
            raise Exception(last_err)

        try:
            url = attempt_download(quality_code)
            if url: return url
        except Exception as e:
            err = e

        if allow_fallback:
            current_qual = quality_code
            
            if current_qual == "27":
                print(f"⚠ Download with quality 27 failed, trying fallback to 7 (24-bit Standard)...")
                try:
                    url = attempt_download("7")
                    if url: 
                        print("✓ Success with fallback quality 7")
                        return url
                except:
                    pass
                current_qual = "7"

            if current_qual == "7":
                print(f"⚠ Download with quality 7 failed, trying fallback to 6 (16-bit Lossless)...")
                try:
                    url = attempt_download("6")
                    if url: 
                        print("✓ Success with fallback quality 6")
                        return url
                except:
                    pass
        
        raise Exception("all APIs and fallbacks failed")

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
                
                total = int(resp.headers.get("Content-Length") or 0)
                downloaded = 0
                with open(temp_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=256 * 1024):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            if self.progress_callback:
                                self.progress_callback(downloaded, total)
                                
            os.replace(temp_path, filepath)
            print(f"\rDownloaded: {downloaded / (1024 * 1024):.2f} MB (Complete)")
        finally:
            if os.path.exists(temp_path):
                try: os.remove(temp_path)
                except: pass

    def download_by_isrc(self, isrc, output_dir, quality, filename_format, include_track_number, position, 
                         spotify_track_name, spotify_artist_name, spotify_album_name, use_album_track_number,
                         **kwargs):
        
        spotify_album_artist = kwargs.get("spotify_album_artist", "Unknown")
        spotify_release_date = kwargs.get("spotify_release_date", "")
        spotify_track_number = kwargs.get("spotify_track_number", 0)
        spotify_disc_number = kwargs.get("spotify_disc_number", 1)
        spotify_total_tracks = kwargs.get("spotify_total_tracks", 0)
        spotify_total_discs = kwargs.get("spotify_total_discs", 1)
        spotify_cover_url = kwargs.get("spotify_cover_url", "")
        spotify_copyright = kwargs.get("spotify_copyright", "")
        spotify_publisher = kwargs.get("spotify_publisher", "")
        spotify_url = kwargs.get("spotify_url", "")
        allow_fallback = kwargs.get("allow_fallback", True)
        use_first_artist_only = kwargs.get("use_first_artist_only", False)

        print(f"Fetching track info for ISRC: {isrc}")
        if output_dir != ".":
            os.makedirs(output_dir, exist_ok=True)

        track = self._search_by_isrc(isrc)
        
        q_track_num = track.get("track_number", 0)
        final_track_num = q_track_num if (use_album_track_number and q_track_num > 0) else position
        if final_track_num == 0 and spotify_track_number > 0:
            final_track_num = spotify_track_number

        print(f"Found track: {spotify_artist_name} - {spotify_track_name}")
        print(f"Album: {spotify_album_name}")
        
        if track.get('hires', False):
            print(f"Quality: Hi-Res ({track.get('maximum_bit_depth', 24)}-bit / {track.get('maximum_sampling_rate', 96.0)} kHz)")
        else:
            print("Quality: Standard")

        # Artist optimization match
        artist_to_use = get_first_artist(spotify_artist_name) if use_first_artist_only else spotify_artist_name
        album_artist_to_use = get_first_artist(spotify_album_artist) if use_first_artist_only else spotify_album_artist
        
        filename = build_qobuz_filename(
            _sanitize_filename(spotify_track_name), 
            _sanitize_filename(artist_to_use), 
            _sanitize_filename(spotify_album_name), 
            _sanitize_filename(album_artist_to_use),
            spotify_release_date, final_track_num, spotify_disc_number, 
            filename_format, include_track_number, position, use_album_track_number
        )
        filepath = os.path.join(output_dir, filename)

        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            size_mb = os.path.getsize(filepath) / (1024 * 1024)
            print(f"File already exists: {filepath} ({size_mb:.2f} MB)")
            return filepath

        print("Getting download URL...")
        download_url = self.get_download_url(track['id'], quality, allow_fallback)
        
        url_preview = download_url[:60] + "..." if len(download_url) > 60 else download_url
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
            "TITLE": spotify_track_name,
            "ARTIST": spotify_artist_name,
            "ALBUM": spotify_album_name,
            "ALBUMARTIST": spotify_album_artist,
            "DATE": spotify_release_date[:4] if len(spotify_release_date) >= 4 else "",
            "TRACKNUMBER": str(final_track_num),
            "TRACKTOTAL": str(spotify_total_tracks),
            "DISCNUMBER": str(spotify_disc_number),
            "DISCTOTAL": str(spotify_total_discs),
            "ISRC": isrc,
            "COPYRIGHT": spotify_copyright,
            "ORGANIZATION": spotify_publisher,
            "URL": spotify_url,
            "DESCRIPTION": "https://github.com/ShuShuzinhuu/SpotiFLAC-Module-Version"
        }

        self._embed_metadata(filepath, metadata, cover_path)
        
        if cover_path and os.path.exists(cover_path):
            try: os.remove(cover_path)
            except: pass

        return filepath

    def _embed_metadata(self, filepath, metadata, cover_path):
        try:
            audio = FLAC(filepath)
            audio.delete() # Limpa as tags originais que a Qobuz envia
            
            for key, val in metadata.items():
                if val and str(val) != "0": 
                    audio[key] = str(val)
            
            if cover_path and os.path.exists(cover_path):
                with open(cover_path, "rb") as img:
                    pic = Picture()
                    pic.data = img.read()
                    pic.type = PictureType.COVER_FRONT
                    pic.mime = "image/jpeg"
                    audio.add_picture(pic)
            
            audio.save()
            print("Metadata embedded successfully!")
        except Exception as e:
            raise Exception(f"failed to embed metadata: {e}")