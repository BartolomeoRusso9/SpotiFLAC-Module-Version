import os
import re
import requests
import time
from typing import Callable, Optional
from mutagen.flac import FLAC, Picture
from mutagen.id3 import PictureType

def sanitize_filename(value: str) -> str:
    """Rimuove i caratteri non validi per i nomi di file su vari OS."""
    return re.sub(r'[\\/*?:"<>|]', "", value).strip()

def safe_int(value) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0

class SpotiDownloader:
    _cached_token = None

    def __init__(self, timeout: float = 15.0):
        self.session = requests.Session()
        # Salvataggio globale del timeout
        self.timeout = timeout 
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })
        self.progress_callback: Optional[Callable[[int, int], None]] = None

    def set_progress_callback(self, callback: Callable[[int, int], None]) -> None:
        self.progress_callback = callback

    def fetch_token(self) -> str:
        if SpotiDownloader._cached_token:
            return SpotiDownloader._cached_token

        print("Recupero del session token...")
        url = "https://spdl.afkarxyz.qzz.io/token"
        
        for attempt in range(1, 4):
            try:
                resp = self.session.get(url, timeout=self.timeout)
                resp.raise_for_status()
                data = resp.json()
                token = data.get("token")
                if token:
                    SpotiDownloader._cached_token = token
                    return token
            except Exception as e:
                if attempt == 3:
                    raise Exception(f"Impossibile ottenere il token dopo 3 tentativi: {e}")
                time.sleep(1)
                
        raise Exception("Token non trovato nella risposta API")

    def get_flac_download_link(self, track_id: str, token: str) -> str:
        print(f"Richiesta link di download FLAC per ID: {track_id}...")
        url = "https://api.spotidownloader.com/download"
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Origin": "https://spotidownloader.com",
            "Referer": "https://spotidownloader.com/"
        }
        
        payload = {"id": track_id, "flac": True}
        
        resp = self.session.post(url, json=payload, headers=headers, timeout=self.timeout)
        
        # Invalida il token se c'è un 401/403 e riprova
        if resp.status_code in (401, 403):
            print("Token scaduto, rigenerazione in corso...")
            SpotiDownloader._cached_token = None
            token = self.fetch_token()
            headers["Authorization"] = f"Bearer {token}"
            resp = self.session.post(url, json=payload, headers=headers, timeout=self.timeout)

        resp.raise_for_status()
        data = resp.json()
        
        if not data.get("success"):
            raise Exception("L'API ha restituito success=false")
            
        flac_link = data.get("linkFlac")
        standard_link = data.get("link")
        final_link = None

        def is_flac(link: str) -> bool:
            if not link:
                return False
            # Ignora la query string per verificare l'estensione reale
            return link.split("?")[0].lower().endswith(".flac")

        if is_flac(flac_link):
            final_link = flac_link
        elif is_flac(standard_link):
            final_link = standard_link
            
        if not final_link:
            raise Exception("L'API non ha restituito un link FLAC (disponibile solo MP3). Elaborazione annullata.")
            
        return final_link

    def _get_max_resolution_cover(self, url: str) -> str:
        if not url:
            return ""
        if "i.scdn.co/image/" in url:
            return re.sub(r'(ab67616d0000)[a-z0-9]+', r'\g<1>b273', url)
        return url

    def _stream_download(self, url: str, filepath: str, token: str) -> None:
        temp_path = filepath + ".part"
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Origin": "https://spotidownloader.com",
            "Referer": "https://spotidownloader.com/"
        }
        
        try:
            # (timeout_connessione, timeout_lettura_stream)
            stream_timeout = (self.timeout, 120.0)
            
            with self.session.get(url, headers=headers, stream=True, timeout=stream_timeout) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("Content-Length") or 0)
                downloaded = 0
                
                with open(temp_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=256 * 1024):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            # Gestione progress callback quando total=0
                            if self.progress_callback:
                                self.progress_callback(downloaded, total if total > 0 else downloaded)
                                
            os.replace(temp_path, filepath)
        except Exception:
            # Pulizia del file .part in caso di interruzione
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise

    def download_by_spotify_id(self, spotify_track_id: str, **kwargs) -> str:
        output_dir = kwargs.get("output_dir", ".")
        os.makedirs(output_dir, exist_ok=True)
        
        track_id = spotify_track_id.split("/")[-1].split("?")[0]
        
        title = kwargs.get("spotify_track_name", "Unknown")
        # Rimosso il .split(",")[0] per supportare nomi con virgole ("Tyler, The Creator")
        artist = kwargs.get("spotify_artist_name", "Unknown")
        track_num = safe_int(kwargs.get("spotify_track_number", 1))
        
        filename_format = kwargs.get("filename_format", "{artist} - {title}")
        raw_filename = filename_format.format(
            artist=artist, 
            title=title, 
            track_num=f"{track_num:02d}"
        )
        
        expected_filename = f"{sanitize_filename(raw_filename)}.flac"
        expected_path = os.path.join(output_dir, expected_filename)

        if os.path.exists(expected_path) and os.path.getsize(expected_path) > 0:
            size_mb = os.path.getsize(expected_path) / (1024 * 1024)
            print(f"File già esistente: {expected_path} ({size_mb:.2f} MB)")
            return expected_path

        token = self.fetch_token()
        flac_url = self.get_flac_download_link(track_id, token)

        print("Avvio download del file FLAC...")
        self._stream_download(flac_url, expected_path, token)
        print("\nDownload completato.") 
        
        self.embed_metadata(expected_path, **kwargs)
        return expected_path

    def embed_metadata(self, filepath: str, **kwargs):
        print("Scrittura metadati e copertina in corso...")
        try:
            cover_url = self._get_max_resolution_cover(kwargs.get("spotify_cover_url"))
            cover_data = None
            cover_mime = "image/jpeg"
            
            if cover_url:
                try: 
                    resp = self.session.get(cover_url, timeout=self.timeout)
                    if resp.status_code == 200: 
                        cover_data = resp.content
                        # Lettura del MIME corretto dagli header della risposta
                        mime = resp.headers.get("Content-Type", "").split(";")[0].strip()
                        if mime in ("image/jpeg", "image/png"):
                            cover_mime = mime
                except Exception as e:
                    print(f"Attenzione: Impossibile scaricare la copertina: {e}")

            audio = FLAC(filepath)
            
            # ATTENZIONE: Questo cancella tutti i tag preesistenti.
            # È utile per pulire la spazzatura lasciata dalle API prima di scrivere i nostri.
            audio.delete() 
            
            audio["TITLE"] = kwargs.get("spotify_track_name", "Unknown")
            audio["ARTIST"] = kwargs.get("spotify_artist_name", "Unknown")
            if kwargs.get("spotify_album_name"): audio["ALBUM"] = kwargs.get("spotify_album_name")
            if kwargs.get("spotify_album_artist"): audio["ALBUMARTIST"] = kwargs.get("spotify_album_artist")
            if kwargs.get("spotify_release_date"): audio["DATE"] = kwargs.get("spotify_release_date")
            
            audio["TRACKNUMBER"] = str(safe_int(kwargs.get("spotify_track_number")) or 1)
            audio["TRACKTOTAL"] = str(safe_int(kwargs.get("spotify_total_tracks")) or 1)
            audio["DISCNUMBER"] = str(safe_int(kwargs.get("spotify_disc_number")) or 1)
            audio["DISCTOTAL"] = str(safe_int(kwargs.get("spotify_total_discs")) or 1)
            
            if kwargs.get("spotify_copyright"): audio["COPYRIGHT"] = kwargs.get("spotify_copyright")
            if kwargs.get("spotify_publisher"): audio["ORGANIZATION"] = kwargs.get("spotify_publisher")
            if kwargs.get("spotify_url"): audio["URL"] = kwargs.get("spotify_url")
            if kwargs.get("spotify_isrc"): audio["ISRC"] = kwargs.get("spotify_isrc")
            if kwargs.get("spotify_upc"): audio["BARCODE"] = kwargs.get("spotify_upc")
            if kwargs.get("spotify_genre"): audio["GENRE"] = kwargs.get("spotify_genre")
            
            audio["DESCRIPTION"] = "Scaricato tramite SpotiFLAC (Python Porting)"

            if cover_data:
                pic = Picture()
                pic.data = cover_data
                pic.type = PictureType.COVER_FRONT
                pic.mime = cover_mime
                audio.add_picture(pic)
            
            audio.save()
            print("Metadati applicati con successo! ✓")

        except Exception as e:
            print(f"Attenzione: Errore durante la scrittura dei metadati: {e}")