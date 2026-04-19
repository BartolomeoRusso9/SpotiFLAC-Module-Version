import os
import re
import requests
from typing import Callable
from urllib.parse import quote
from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TALB, TPE2, TDRC, TRCK, TPOS, APIC, TPUB, WXXX, COMM
from mutagen.mp3 import MP3

def sanitize_filename(value: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", value).strip()

def safe_int(value) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0

class YouTubeDownloader:
    def __init__(self, timeout: float = 120.0):
        self.session = requests.Session()
        self.session.timeout = timeout
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
        })
        self.progress_callback: Callable[[int, int], None] = None

    def set_progress_callback(self, callback: Callable[[int, int], None]) -> None:
        self.progress_callback = callback

    def get_youtube_url_from_spotify(self, spotify_track_id: str, track_name: str = None, artist_name: str = None) -> str:
        print("Fetching YouTube URL via Songlink HTML...")

        url = f"https://song.link/s/{spotify_track_id}"
        headers = {"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"}

        try:
            # ATTEMPT 1: Try to extract from Songlink
            resp = self.session.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            html = resp.text

            match = re.search(r'https://(?:music\.)?youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})', html)
            if not match:
                match = re.search(r'https://youtu\.be/([a-zA-Z0-9_-]{11})', html)

            if match:
                video_id = match.group(1)
                yt_url = f"https://music.youtube.com/watch?v={video_id}"
                print(f"✓ Found on Songlink: {yt_url}")
                return yt_url
            else:
                print("[!] Songlink does not have a YouTube link for this track.")

        except Exception as e:
            print(f"[!] Error accessing Songlink: {e}")

        # ATTEMPT 2: Direct YouTube Fallback (Text Search)
        print("Starting direct YouTube search (Fallback)...")
        if track_name and artist_name:
            yt_url = self._search_youtube_direct(track_name, artist_name)
            if yt_url:
                return yt_url
                
        raise Exception("Failed to resolve YouTube URL: Songlink failed and direct search did not find the track.")

    def _search_youtube_direct(self, track_name: str, artist_name: str) -> str:
        """
        Performs a silent search on YouTube and grabs the first video ID.
        This prevents reliance on Songlink for old/famous tracks.
        """
        # "audio" helps filter out music videos with long intros
        query = quote(f"{track_name} {artist_name} audio")
        search_url = f"https://www.youtube.com/results?search_query={query}"
        
        try:
            resp = self.session.get(search_url, timeout=10)
            resp.raise_for_status()
            
            # YouTube stores search data inside a JS variable called ytInitialData
            # This regex captures the first Video ID that appears on the search page
            match = re.search(r'"videoId":"([a-zA-Z0-9_-]{11})"', resp.text)
            if match:
                video_id = match.group(1)
                yt_url = f"https://music.youtube.com/watch?v={video_id}"
                print(f"✓ Video found via YouTube Search: {yt_url}")
                return yt_url
                
        except Exception as e:
            print(f"Error in direct YouTube search: {e}")
            
        return None

    def _extract_video_id(self, url: str) -> str:
        match = re.search(r'(?:v=|/v/|youtu\.be/|/embed/)([a-zA-Z0-9_-]{11})', url)
        return match.group(1) if match else None

    def _request_spotube_dl(self, video_id: str, audio_format="mp3", bitrate="320"):
        engines = ["v1", "v3", "v2"]
        for engine in engines:
            api_url = f"https://spotubedl.com/api/download/{video_id}?engine={engine}&format={audio_format}&quality={bitrate}"
            try:
                print(f"Fetching from SpotubeDL (Engine: {engine})...")
                resp = self.session.get(api_url, timeout=15)
                if resp.status_code == 200:
                    data = resp.json()
                    download_url = data.get("url")
                    
                    if download_url:
                        if download_url.startswith("/"):
                            download_url = "https://spotubedl.com" + download_url
                            
                        return download_url
            except Exception:
                continue
        return None

    def _request_cobalt(self, video_id: str, audio_format="mp3", bitrate="320"):
        print("SpotubeDL failed. Trying Cobalt API (Fallback)...")
        api_url = "https://api.qwkuns.me"
        payload = {
            "url": f"https://music.youtube.com/watch?v={video_id}",
            "audioFormat": audio_format,
            "audioBitrate": str(bitrate),
            "downloadMode": "audio",
            "filenameStyle": "basic",
            "disableMetadata": True
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        try:
            resp = self.session.post(api_url, json=payload, headers=headers, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") in ["tunnel", "redirect"] and data.get("url"):
                    return data["url"]
        except Exception:
            pass
        return None

    def download_by_spotify_id(self, spotify_track_id, **kwargs):
        output_dir = kwargs.get("output_dir", ".")
        os.makedirs(output_dir, exist_ok=True)
        
        # Passing names in case Songlink fails!
        yt_url = self.get_youtube_url_from_spotify(
            spotify_track_id,
            track_name=kwargs.get("spotify_track_name"),
            artist_name=kwargs.get("spotify_artist_name")
        )
        
        video_id = self._extract_video_id(yt_url)
        if not video_id:
            raise Exception("Could not extract video ID.")

        safe_title = sanitize_filename(kwargs.get("spotify_track_name", "Unknown"))
        safe_artist = sanitize_filename(kwargs.get("spotify_artist_name", "Unknown").split(",")[0])
        
        expected_filename = f"{safe_artist} - {safe_title}.mp3" 
        expected_path = os.path.join(output_dir, expected_filename)

        if os.path.exists(expected_path) and os.path.getsize(expected_path) > 0:
            print(f"File already exists: {expected_path}")
            return expected_path

        download_url = self._request_spotube_dl(video_id, "mp3", "320")
        if not download_url:
            download_url = self._request_cobalt(video_id, "mp3", "320")
            
        if not download_url:
            raise Exception("All YouTube download APIs failed.")

        print("Downloading track from YouTube...")
        with self.session.get(download_url, stream=True) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or 0)
            downloaded = 0
            with open(expected_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if self.progress_callback:
                            self.progress_callback(downloaded, total)
        print()

        self.embed_metadata(
            expected_path, 
            kwargs.get("spotify_track_name"), kwargs.get("spotify_artist_name"),
            kwargs.get("spotify_album_name"), kwargs.get("spotify_album_artist"),
            kwargs.get("spotify_release_date"), kwargs.get("spotify_track_number", 1),
            kwargs.get("spotify_total_tracks", 1), kwargs.get("spotify_disc_number", 1),
            kwargs.get("spotify_total_discs", 1), kwargs.get("spotify_cover_url"),
            kwargs.get("spotify_publisher"), kwargs.get("spotify_url") 
        )

        return expected_path

    def embed_metadata(self, filepath, title, artist, album, album_artist, date, track_num, total_tracks, disc_num, total_discs, cover_url, publisher=None, url=None):
        print("Embedding metadata and cover art...")
        try:
            try:
                audio = ID3(filepath)
                audio.delete() 
            except ID3NoHeaderError:
                audio = ID3()
            
            if title: audio.add(TIT2(encoding=3, text=str(title)))
            if artist: audio.add(TPE1(encoding=3, text=str(artist)))
            if album: audio.add(TALB(encoding=3, text=str(album)))
            if album_artist: audio.add(TPE2(encoding=3, text=str(album_artist)))
            if date: audio.add(TDRC(encoding=3, text=str(date)))
            
            audio.add(TRCK(encoding=3, text=f"{safe_int(track_num)}/{safe_int(total_tracks)}"))
            audio.add(TPOS(encoding=3, text=f"{safe_int(disc_num)}/{safe_int(total_discs)}"))

            if publisher: 
                audio.add(TPUB(encoding=3, text=[str(publisher)]))
            if url: 
                audio.add(WXXX(encoding=3, desc=u'', url=str(url)))
                
            audio.add(COMM(
                encoding=3, 
                lang='eng', 
                desc=u'', 
                text=[u"https://github.com/ShuShuzinhuu/SpotiFLAC-Module-Version"]
            ))

            if cover_url:
                try:
                    resp = self.session.get(cover_url, timeout=10)
                    if resp.status_code == 200:
                        audio.add(APIC(
                            encoding=3, 
                            mime='image/jpeg', 
                            type=3, 
                            desc='Cover', 
                            data=resp.content
                        ))
                except Exception as e:
                    print(f"Warning: Could not download cover: {e}")

            audio.save(filepath, v2_version=3)
            print("Metadata embedded successfully")
            
        except Exception as e:
            print(f"Warning: Failed to embed metadata: {e}")