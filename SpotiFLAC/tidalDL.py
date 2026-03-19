import base64
import json
import os
import random
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType

class ProgressCallback:
    def __call__(self, current: int, total: int) -> None:
        if total > 0:
            percent = (current / total) * 100
            print(f"\r{percent:.2f}% ({current}/{total})", end="")
        else:
            print(f"\r{current / (1024 * 1024):.2f} MB", end="")

def sanitize_filename(value: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", value).strip()

def get_first_artist(artist_str: str) -> str:
    if not artist_str:
        return "Unknown"
    return artist_str.split(",")[0].strip()

def safe_int(value) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0

def build_tidal_filename(title, artist, album, album_artist, release_date, track_number, disc_number, format_string, include_track_number, position, use_album_track_number):
    number_to_use = track_number if use_album_track_number and track_number > 0 else position
    year = release_date[:4] if len(release_date) >= 4 else ""

    if "{" in format_string:
        filename = (format_string.replace("{title}", title)
                    .replace("{artist}", artist)
                    .replace("{album}", album)
                    .replace("{album_artist}", album_artist)
                    .replace("{year}", year)
                    .replace("{date}", sanitize_filename(release_date)))
        
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

    return sanitize_filename(filename) + ".flac"

def parse_manifest(manifest_b64: str) -> Tuple[str, str, List[str], str]:
    try:
        manifest_bytes = base64.b64decode(manifest_b64)
    except Exception as exc:
        raise Exception(f"failed to decode manifest: {exc}")

    manifest_str = manifest_bytes.decode(errors="ignore").strip()
    
    # Formato BTS (JSON)
    if manifest_str.startswith("{"):
        try:
            data = json.loads(manifest_str)
            urls = data.get("urls", [])
            mime_type = data.get("mimeType", "")
            if urls:
                print(f"Manifest: BTS format ({mime_type})")
                return urls[0], "", [], mime_type
            raise Exception("no URLs in BTS manifest")
        except Exception as exc:
            raise Exception(f"failed to parse BTS manifest: {exc}")

    # Formato DASH (XML)
    print("Manifest: DASH format")
    init_url = ""
    media_template = ""
    segment_count = 0

    try:
        mpd = ET.fromstring(manifest_str)
        ns = {"mpd": mpd.tag.split("}")[0].strip("{")} if "}" in mpd.tag else {}
        seg_template = mpd.find(".//mpd:SegmentTemplate", ns)
        if seg_template is None:
            seg_template = mpd.find(".//SegmentTemplate")
            
        if seg_template is not None:
            init_url = seg_template.get("initialization", "")
            media_template = seg_template.get("media", "")
            timeline = seg_template.find("mpd:SegmentTimeline", ns) or seg_template.find("SegmentTimeline")
            
            if timeline is not None:
                segments = []
                for seg in timeline.findall("mpd:S", ns) or timeline.findall("S"):
                    repeat = int(seg.get("r") or 0)
                    segments.append(repeat + 1)
                segment_count = sum(segments)

    except Exception:
        pass

    # Regex Fallback caso o XML falhe
    if not init_url or not media_template or segment_count == 0:
        print("Using regex fallback for DASH manifest...")
        init_match = re.search(r'initialization="([^"]+)"', manifest_str)
        media_match = re.search(r'media="([^"]+)"', manifest_str)
        
        if init_match: init_url = init_match.group(1)
        if media_match: media_template = media_match.group(1)
        
        matches = re.findall(r'<S\s+[^>]*>', manifest_str)
        for match in matches:
            r_match = re.search(r'r="(\d+)"', match)
            repeat = int(r_match.group(1)) if r_match else 0
            segment_count += repeat + 1

    if not init_url:
        raise Exception("no initialization URL found in manifest")
    
    if segment_count == 0:
        raise Exception("no segments found in manifest")

    init_url = init_url.replace("&", "&")
    media_template = media_template.replace("&", "&")

    media_urls = []
    for i in range(1, segment_count + 1):
        media_urls.append(media_template.replace("$Number$", str(i)))

    return "", init_url, media_urls, ""


class TidalDownloader:
    def __init__(self, timeout: float = 15.0):
        self.session = requests.Session()
        self.session.timeout = timeout
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
        })
        self.progress_callback: Callable[[int, int], None] = ProgressCallback()
        
        self.apis = [
            "https://hifi-one.spotisaver.net",
            "https://hifi-two.spotisaver.net",
            "https://eu-central.monochrome.tf",
            "https://us-west.monochrome.tf",
            "https://api.monochrome.tf",
            "https://monochrome-api.samidy.com",
            "https://tidal.kinoplus.online"
        ]

    def set_progress_callback(self, callback: Callable[[int, int], None]) -> None:
        self.progress_callback = callback

    def get_tidal_url_from_spotify(self, spotify_track_id: str) -> str:

        print("Getting Tidal URL via Songlink HTML...")

        url = f"https://song.link/s/{spotify_track_id}"

        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept-Language": "en-US,en;q=0.9"
        }

        try:
            resp = self.session.get(url, headers=headers, timeout=10)
            resp.raise_for_status()

            html = resp.text

            match = re.search(
                r'https://listen\.tidal\.com/(track|album)/([0-9]+)',
                html
            )

            if not match:
                raise Exception("Tidal link not found in HTML")

            tidal_url = match.group(0)

            print(f"Found Tidal URL: {tidal_url}")

            return tidal_url

        except Exception as e:
            raise Exception(f"Error resolving Tidal URL: {e}")

    def get_track_id_from_url(self, tidal_url: str) -> int:
        parts = tidal_url.split("/track/")
        if len(parts) < 2:
            raise Exception("invalid tidal URL format")
        track_part = parts[1].split("?")[0].strip()
        try:
            return int(track_part)
        except ValueError as exc:
            raise Exception(f"failed to parse track ID: {exc}")

    def get_download_url_rotated(self, track_id: int, quality: str) -> Tuple[str, str]:
        if not self.apis:
            raise Exception("no APIs available")

        apis_shuffled = self.apis.copy()
        random.seed(time.time())
        random.shuffle(apis_shuffled)

        print(f"Rotating through {len(apis_shuffled)} APIs...")
        
        last_error = None
        for api_url in apis_shuffled:
            print(f"Trying API: {api_url}")
            url = f"{api_url}/track/?id={track_id}&quality={quality}"
            
            try:
                resp = self.session.get(url, timeout=15.0)
                if resp.status_code != 200:
                    last_error = f"HTTP {resp.status_code}"
                    continue
                
                body = resp.json()
                
                # Check v2 response first
                if isinstance(body, dict) and body.get("data", {}).get("manifest"):
                    print(f"✓ Success with: {api_url}")
                    return api_url, "MANIFEST:" + body["data"]["manifest"]
                
                # Check v1 response
                if isinstance(body, list):
                    for item in body:
                        if item.get("OriginalTrackUrl"):
                            print(f"✓ Success with: {api_url}")
                            return api_url, item["OriginalTrackUrl"]

                last_error = "no download URL or manifest in response"
                
            except Exception as e:
                last_error = str(e)
                continue

        raise Exception(f"all APIs failed. Last error: {last_error}")

    def _stream_download(self, url: str, filepath: str) -> None:
        temp_path = filepath + ".part"
        with self.session.get(url, stream=True, timeout=120) as resp:
            resp.raise_for_status()
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

    def download_from_manifest(self, manifest_b64: str, output_path: str) -> None:
        direct_url, init_url, media_urls, mime_type = parse_manifest(manifest_b64)
        
        if direct_url and ("flac" in mime_type.lower() or not mime_type):
            print("Downloading file...")
            self._stream_download(direct_url, output_path)
            print("\nDownload complete")
            return

        temp_path = output_path + ".m4a.tmp"

        if direct_url:
            print(f"Downloading non-FLAC file ({mime_type})...")
            self._stream_download(direct_url, temp_path)
            print("\nDownload complete")
        else:
            total_segments = len(media_urls)
            print(f"Downloading {total_segments + 1} segments...")
            
            with open(temp_path, "wb") as f:
                print("Downloading init segment... ", end="", flush=True)
                resp = self.session.get(init_url)
                resp.raise_for_status()
                f.write(resp.content)
                print("OK")

                total_bytes = 0
                for i, media_url in enumerate(media_urls):
                    resp = self.session.get(media_url)
                    resp.raise_for_status()
                    f.write(resp.content)
                    total_bytes += len(resp.content)
                    
                    mb_downloaded = total_bytes / (1024 * 1024)
                    print(f"\rDownloading: {mb_downloaded:.2f} MB ({i+1}/{total_segments} segments)", end="")
            
            print(f"\nDownloaded: {os.path.getsize(temp_path) / (1024 * 1024):.2f} MB (Complete)")

        print("Converting to FLAC...")
        cmd = ["ffmpeg", "-y", "-i", temp_path, "-vn", "-c:a", "flac", output_path]
        
        # Ocultar janela no Windows
        si = None
        if os.name == 'nt':
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        result = subprocess.run(cmd, capture_output=True, text=True, startupinfo=si)
        
        if result.returncode != 0:
            m4a_path = output_path.replace(".flac", ".m4a")
            os.replace(temp_path, m4a_path)
            raise Exception(f"ffmpeg conversion failed (M4A saved as {os.path.basename(m4a_path)}): {result.stderr}")
            
        try: os.remove(temp_path)
        except: pass
        print("Download complete")

    def download_file(self, url: str, filepath: str) -> None:
        if url.startswith("MANIFEST:"):
            self.download_from_manifest(url.replace("MANIFEST:", "", 1), filepath)
        else:
            self._stream_download(url, filepath)

    def embed_metadata(self, filepath, title, artist, album, album_artist, date, track_num, total_tracks, 
                       disc_num, total_discs, cover_url, copyright, publisher, url):
        print("Embedding metadata and cover art...")
        try:
            cover_data = None
            if cover_url:
                try: 
                    resp = self.session.get(cover_url, timeout=15)
                    if resp.status_code == 200:
                        cover_data = resp.content
                except Exception as e:
                    print(f"Warning: Could not download cover: {e}")

            t_num = safe_int(track_num)
            t_total = safe_int(total_tracks)
            d_num = safe_int(disc_num)
            d_total = safe_int(total_discs)
            
            if t_num == 0: t_num = 1
            if d_num == 0: d_num = 1

            audio = FLAC(filepath)
            audio.delete()
            
            audio["TITLE"] = title
            audio["ARTIST"] = artist
            audio["ALBUM"] = album
            audio["ALBUMARTIST"] = album_artist
            audio["DATE"] = date
            audio["TRACKNUMBER"] = str(t_num)
            audio["TRACKTOTAL"] = str(t_total)
            audio["DISCNUMBER"] = str(d_num)
            audio["DISCTOTAL"] = str(d_total)
            if copyright: audio["COPYRIGHT"] = copyright
            if publisher: audio["ORGANIZATION"] = publisher
            if url: audio["URL"] = url
            audio["DESCRIPTION"] = "https://github.com/ShuShuzinhuu/SpotiFLAC-Module-Version"

            if cover_data:
                pic = Picture()
                pic.data = cover_data
                pic.type = PictureType.COVER_FRONT
                pic.mime = "image/jpeg"
                audio.add_picture(pic)
            audio.save()
            print("Metadata embedded successfully")

        except Exception as e:
            print(f"Warning: Failed to embed metadata: {e}")

    def download_by_url(self, tidal_url: str, output_dir: str, quality: str, filename_format: str, 
                        include_track_number: bool, position: int, 
                        spotify_track_name: str, spotify_artist_name: str, spotify_album_name: str, 
                        spotify_album_artist: str, spotify_release_date: str, spotify_cover_url: str, 
                        spotify_track_number: int, spotify_disc_number: int, spotify_total_tracks: int, 
                        embed_max_quality_cover: bool, spotify_total_discs: int, spotify_copyright: str, 
                        spotify_publisher: str, spotify_url: str, use_album_track_number: bool = False,
                        use_first_artist_only: bool = False, allow_fallback: bool = True):
        
        os.makedirs(output_dir, exist_ok=True)
        print(f"Using Tidal URL: {tidal_url}")

        track_id = self.get_track_id_from_url(tidal_url)

        artist_to_use = get_first_artist(spotify_artist_name) if use_first_artist_only else spotify_artist_name
        album_artist_to_use = get_first_artist(spotify_album_artist) if use_first_artist_only else spotify_album_artist

        filename = build_tidal_filename(
            sanitize_filename(spotify_track_name), 
            sanitize_filename(artist_to_use), 
            sanitize_filename(spotify_album_name), 
            sanitize_filename(album_artist_to_use),
            spotify_release_date, spotify_track_number, spotify_disc_number, 
            filename_format, include_track_number, position, use_album_track_number
        )
        filepath = os.path.join(output_dir, filename)

        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            size_mb = os.path.getsize(filepath) / (1024 * 1024)
            print(f"File already exists: {filepath} ({size_mb:.2f} MB)")
            return filepath

        try:
            success_api, download_url = self.get_download_url_rotated(track_id, quality)
        except Exception as e:
            if quality == "HI_RES" and allow_fallback:
                print("⚠ HI_RES unavailable/failed on all APIs, falling back to LOSSLESS...")
                try:
                    success_api, download_url = self.get_download_url_rotated(track_id, "LOSSLESS")
                except Exception as e2:
                    raise Exception(f"failed to get download URL (HI_RES & LOSSLESS both failed): {e2}")
            else:
                raise e

        print(f"Downloading to: {filepath}")
        self.download_file(download_url, filepath)

        self.embed_metadata(filepath, spotify_track_name, spotify_artist_name, spotify_album_name, 
                            spotify_album_artist, spotify_release_date, spotify_track_number, 
                            spotify_total_tracks, spotify_disc_number, spotify_total_discs, 
                            spotify_cover_url, spotify_copyright, spotify_publisher, spotify_url)

        print("Done\n✓ Downloaded successfully from Tidal")
        return filepath

    def download_by_spotify_id(self, spotify_track_id, **kwargs):
        tidal_url = self.get_tidal_url_from_spotify(spotify_track_id)
        spotify_url = f"https://open.spotify.com/track/{spotify_track_id}"
        
        default_kwargs = {
            "output_dir": ".", "quality": "LOSSLESS", "filename_format": "{title} - {artist}",
            "include_track_number": False, "position": 1,
            "spotify_track_name": "Unknown", "spotify_artist_name": "Unknown", 
            "spotify_album_name": "Unknown", "spotify_album_artist": "Unknown",
            "spotify_release_date": "", "spotify_cover_url": "", "spotify_track_number": 1,
            "spotify_disc_number": 1, "spotify_total_tracks": 1, "embed_max_quality_cover": True,
            "spotify_total_discs": 1, "spotify_copyright": "", "spotify_publisher": "", "spotify_url": spotify_url,
            "use_album_track_number": False, "use_first_artist_only": False, "allow_fallback": True
        }

        for key in kwargs:
            if key in default_kwargs:
                default_kwargs[key] = kwargs[key]

        return self.download_by_url(tidal_url, **default_kwargs)