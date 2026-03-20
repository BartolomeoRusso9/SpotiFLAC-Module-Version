import os
import re
from tempfile import template
import time
import argparse
import asyncio
import shutil 
from dataclasses import dataclass, field
from SpotiFLAC.youtubeDL import YouTubeDownloader
from SpotiFLAC.getMetadata import get_filtered_data, parse_uri, SpotifyInvalidUrlException
from SpotiFLAC.tidalDL import TidalDownloader
from SpotiFLAC.deezerDL import DeezerDownloader
from SpotiFLAC.qobuzDL import QobuzDownloader
from SpotiFLAC.amazonDL import AmazonDownloader
from SpotiFLAC.progress import DownloadManager, RichProgressCallback


@dataclass
class Config:
    url: str
    output_dir: str
    service: list = None
    filename_format: str = "{title} - {artist}"
    use_track_numbers: bool = False
    use_artist_subfolders: bool = False
    use_album_subfolders: bool = False
    is_album: bool = False
    is_playlist: bool = False
    is_single_track: bool = False
    album_or_playlist_name: str = ""
    tracks: list = field(default_factory=list)
    worker: object = None
    loop: int = 3600
    start_time: float = 0.0
    end_time: float = 0.0


@dataclass
class Track:
    external_urls: str
    title: str
    artists: str
    album: str
    album_artist: str 
    track_number: int
    duration_ms: int
    id: str
    isrc: str = ""
    release_date: str = ""
    cover_url: str = ""
    downloaded: bool = False


# --- FUNÇÕES AUXILIARES ---

def extract_cover_art(data, key_primary="images", key_secondary="album"):
    img_data = data.get(key_primary)
    
    if img_data and isinstance(img_data, str):
        return img_data
        
    if img_data and isinstance(img_data, list) and len(img_data) > 0:
        if isinstance(img_data[0], dict):
            return img_data[0].get("url", "")
        if isinstance(img_data[0], str):
            return img_data[0]

    if key_secondary and key_secondary in data:
        album_data = data[key_secondary]
        if isinstance(album_data, dict):
            return extract_cover_art(album_data, "images", None)
            
    return ""


def format_artists(artists_list):
    if isinstance(artists_list, list):
        return ", ".join([a.get("name", "Unknown") if isinstance(a, dict) else str(a) for a in artists_list])
    return str(artists_list) if artists_list else "Unknown Artist"


def get_metadata(url):
    try:
        metadata = get_filtered_data(url)
        if "error" in metadata:
            print("Error fetching metadata:", metadata["error"])
        else:
            print("Metadata fetched successfully.")
            return metadata
    except SpotifyInvalidUrlException as e:
        print("Invalid URL:", str(e))
    except Exception as e:
        print("An error occurred while fetching metadata:", str(e))


def fetch_tracks(url):
    if not url:
        print('Warning: Please enter a Spotify URL.')
        return

    try:
        print('Just a moment. Fetching metadata...')
        metadata = get_metadata(url)
        if metadata:
            on_metadata_fetched(metadata)
        else:
            print("Error: Empty metadata received.")
    except Exception as e:
        print(f'Error: Failed to start metadata fetch: {str(e)}')


def on_metadata_fetched(metadata):
    try:
        url_info = parse_uri(config.url)

        if url_info["type"] == "track":
            data = metadata.get("track", metadata)
            handle_track_metadata(data)
        elif url_info["type"] == "album":
            handle_album_metadata(metadata)
        elif url_info["type"] == "playlist":
            handle_playlist_metadata(metadata)

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f'Error parsing metadata: {str(e)}')


def handle_track_metadata(track_data):
    track_id = track_data.get("id")
    if not track_id and "external_urls" in track_data:
        ext = track_data["external_urls"]
        if isinstance(ext, dict): track_id = ext.get("spotify", "").split("/")[-1]
        elif isinstance(ext, str): track_id = ext.split("/")[-1]

    if not track_id:
        print("[!] Skipping track without ID")
        return

    cover = extract_cover_art(track_data)
    
    artist_names = format_artists(track_data.get("artists", []))
    
    album_obj = track_data.get("album", {})
    if isinstance(album_obj, dict) and album_obj.get("artists"):
        album_artist = format_artists(album_obj.get("artists"))
    else:
        album_artist = artist_names

    track = Track(
        external_urls=f"https://open.spotify.com/track/{track_id}",
        title=track_data.get("name", "Unknown Title"),
        artists=artist_names,
        album=track_data.get("album_name", track_data.get("album", {}).get("name", "Unknown Album")),
        album_artist=album_artist,
        track_number=track_data.get("track_number", 1),
        duration_ms=track_data.get("duration_ms", 0),
        id=track_id,
        isrc=track_data.get("external_ids", {}).get("isrc", "") or track_data.get("isrc", ""),
        release_date=track_data.get("album", {}).get("release_date", "") or track_data.get("release_date", ""),
        cover_url=cover 
    )

    config.tracks = [track]
    config.is_single_track = True
    config.is_album = config.is_playlist = False
    config.album_or_playlist_name = f"{config.tracks[0].title} - {config.tracks[0].artists}"


def handle_album_metadata(album_data):
    config.album_or_playlist_name = album_data.get("album_info", {}).get("name", album_data.get("name", "Unknown Album"))
    album_release_date = album_data.get("album_info", {}).get("release_date", album_data.get("release_date", ""))
    
    raw_album_artists = album_data.get("album_info", {}).get("artists", [])
    if not raw_album_artists:
         raw_album_artists = album_data.get("artists", [])
    
    if isinstance(raw_album_artists, str):
        main_album_artist = raw_album_artists
    else:
        main_album_artist = format_artists(raw_album_artists)

    album_cover = extract_cover_art(album_data.get("album_info", album_data))
    tracks_raw = album_data.get("track_list", album_data.get("tracks", {}).get("items", []))

    for track in tracks_raw:
        track_id = track.get("id")
        if not track_id and "external_urls" in track:
            ext = track["external_urls"]
            if isinstance(ext, dict): track_id = ext.get("spotify", "").split("/")[-1]
            elif isinstance(ext, str): track_id = ext.split("/")[-1]

        if not track_id or any(t.id == track_id for t in config.tracks):
            continue

        track_cover = extract_cover_art(track)
        if not track_cover:
            track_cover = album_cover

        artist_names = format_artists(track.get("artists", []))

        config.tracks.append(Track(
            external_urls=f"https://open.spotify.com/track/{track_id}",
            title=track.get("name", "Unknown Title"),
            artists=artist_names,
            album=config.album_or_playlist_name,
            album_artist=main_album_artist,
            track_number=track.get("track_number", 0),
            duration_ms=track.get("duration_ms", 0),
            id=track_id,
            isrc=track.get("isrc", ""), 
            release_date=album_release_date,
            cover_url=track_cover
        ))

    config.is_album = True
    config.is_playlist = config.is_single_track = False


def handle_playlist_metadata(playlist_data):
    info = playlist_data.get("playlist_info", playlist_data)
    config.album_or_playlist_name = info.get("name", "Unknown Playlist")
    
    playlist_cover = extract_cover_art(info)
    
    tracks_raw = playlist_data.get("track_list", [])
    if not tracks_raw and "tracks" in playlist_data:
        tracks_raw = playlist_data["tracks"].get("items", [])

    for item in tracks_raw:
        track = item.get("track", item)
        if not track: continue 
        
        track_id = track.get("id")
        if not track_id and "external_urls" in track:
            ext = track["external_urls"]
            if isinstance(ext, dict): track_id = ext.get("spotify", "").split("/")[-1]
            elif isinstance(ext, str): track_id = ext.split("/")[-1]
            
        if not track_id or any(t.id == track_id for t in config.tracks):
            continue
        
        track_cover = extract_cover_art(track)
        if not track_cover:
            track_cover = playlist_cover
        
        artist_names = format_artists(track.get("artists", []))
        
        alb = track.get("album", {})
        album_name = alb.get("name", track.get("album_name", "Unknown Album"))
        
        if alb.get("artists"):
            album_artist = format_artists(alb.get("artists"))
        else:
            album_artist = artist_names

        release_date = alb.get("release_date", "")

        config.tracks.append(Track(
            external_urls=f"https://open.spotify.com/track/{track_id}",
            title=track.get("name", "Unknown Title"),
            artists=artist_names,
            album=album_name,
            album_artist=album_artist,
            track_number=track.get("track_number", len(config.tracks) + 1),
            duration_ms=track.get("duration_ms", 0),
            id=track_id,
            isrc=track.get("isrc", ""),
            release_date=release_date,
            cover_url=track_cover
        ))

    config.is_playlist = True
    config.is_album = config.is_single_track = False


def download_tracks(indices):
    if not config.tracks:
        print("No tracks found to download.")
        return

    raw_outpath = config.output_dir
    outpath = os.path.normpath(raw_outpath)
    if not os.path.exists(outpath):
        print('Warning: Invalid output directory. Please check if the folder exists.')
        return

    tracks_to_download = config.tracks if config.is_single_track else [config.tracks[i] for i in indices]

    if config.is_album or config.is_playlist:
        name = config.album_or_playlist_name.strip()
        folder_name = re.sub(r'[<>:"/\\|?*]', '_', name)
        outpath = os.path.join(outpath, folder_name)
        os.makedirs(outpath, exist_ok=True)

    manager = DownloadManager()
    for track in tracks_to_download:
        manager.add_to_queue(
            item_id=track.id,
            track_name=track.title,
            artist_name=track.artists,
            album_name=track.album,
            spotify_id=track.id
        )

    try:
        start_download_worker(tracks_to_download, outpath)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Error starting download: {str(e)}")


def start_download_worker(tracks_to_download, outpath):
    config.worker = DownloadWorker(
        tracks_to_download,
        outpath,
        config.is_single_track,
        config.is_album,
        config.is_playlist,
        config.album_or_playlist_name,
        config.filename_format,
        config.use_track_numbers,
        config.use_artist_subfolders,
        config.use_album_subfolders,
        config.service,
    )
    config.worker.run()


def on_download_finished(success, message, failed_tracks, total_elapsed=None):
    if success:
        print(f"\n=======================================")
        print(f"\nStatus: {message}")
        if failed_tracks:
            print("\nFailed downloads:")
            for title, artists, error in failed_tracks:
                print(f"• {title} - {artists}")
                print(f"  Error: {error}\n")
    else:
        print(f"Error: {message}")

    if total_elapsed is not None:
        print(f"\nElapsed time for this download loop: {format_seconds(total_elapsed)}")

    if config.loop is not None and config.loop > 0:
        print(f"\nDownload starting again in: {format_minutes(config.loop)}")
        print(f"\n=======================================")
        time.sleep(config.loop * 60)
        fetch_tracks(config.url)
        download_tracks(range(len(config.tracks)))


def update_progress(message):
    print(message)


def format_minutes(minutes):
    if not isinstance(minutes, (int, float)):
        return f"{minutes} (invalid format)"
        
    if minutes < 60:
        return f"{minutes} minutes"
    elif minutes < 1440:
        hours = minutes // 60
        mins = minutes % 60
        return f"{hours} hours {mins} minutes"
    else:
        days = minutes // 1440
        hours = (minutes % 1440) // 60
        mins = minutes % 60
        return f"{days} days {hours} hours {mins} minutes"


def format_seconds(seconds: float) -> str:
    seconds = int(round(seconds))
    days, rem = divmod(seconds, 86400)
    hrs, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    parts = []
    if days: parts.append(f"{days}d")
    if hrs: parts.append(f"{hrs}h")
    if mins: parts.append(f"{mins}m")
    if secs or not parts: parts.append(f"{secs}s")
    return " ".join(parts)


def sanitize_filename_component(value: str) -> str:
    if not value: return ""
    sanitized = re.sub(r'[<>:"/\\|?*]', lambda m: "'" if m.group() == '"' else '_', value)
    sanitized = re.sub(r'\s+', ' ', sanitized).strip()
    return sanitized


def format_custom_filename(template: str, track, position: int = 1, ext: str = ".flac") -> str:
    year = ""
    if track.release_date:
        year = track.release_date.split("-")[0] if "-" in track.release_date else track.release_date

    duration = ""
    if track.duration_ms:
        total_seconds = track.duration_ms // 1000
        minutes = total_seconds // 60
        seconds = total_seconds % 60
        duration = f"{minutes:02d}:{seconds:02d}"

    replacements = {
        "title": sanitize_filename_component(track.title),
        "artist": sanitize_filename_component(track.artists),
        "album": sanitize_filename_component(track.album),
        "track_number": f"{track.track_number:02d}" if track.track_number else f"{position:02d}",
        "track": f"{track.track_number:02d}" if track.track_number else f"{position:02d}",
        "date": sanitize_filename_component(track.release_date),
        "year": year,
        "position": f"{position:02d}",
        "isrc": sanitize_filename_component(track.isrc),
        "duration": duration,
    }

    result = template
    for key, value in replacements.items():
        result = result.replace(f"{{{key}}}", value)

    # Troque a checagem fixa pela extensão que veio no parâmetro
    if not result.lower().endswith(ext):
        result += ext
    return re.sub(r'\s+', ' ', result).strip()


class DownloadWorker:
    def __init__(self, tracks, outpath, is_single_track=False, is_album=False, is_playlist=False,
                 album_or_playlist_name='', filename_format='{title} - {artist}', use_track_numbers=True,
                 use_artist_subfolders=False, use_album_subfolders=False, services=["tidal"]):
        super().__init__()
        self.tracks = tracks
        self.outpath = outpath
        self.is_single_track = is_single_track
        self.is_album = is_album
        self.is_playlist = is_playlist
        self.album_or_playlist_name = album_or_playlist_name
        self.filename_format = filename_format
        self.use_track_numbers = use_track_numbers
        self.use_artist_subfolders = use_artist_subfolders
        self.use_album_subfolders = use_album_subfolders
        self.services = services
        self.failed_tracks = []

    def get_formatted_filename(self, track, position=1, ext=".flac"):
        if self.filename_format in ["title_artist", "artist_title", "title_only"]:
            if self.filename_format == "artist_title":
                filename = f"{track.artists} - {track.title}{ext}"
            elif self.filename_format == "title_only":
                filename = f"{track.title}{ext}"
            else:
                filename = f"{track.title} - {track.artists}{ext}"
            return re.sub(r'[<>:"/\\|?*]', lambda m: "'" if m.group() == '"' else '_', filename)
        return format_custom_filename(self.filename_format, track, position, ext)

    def run(self):
        try:
            total_tracks = len(self.tracks)
            start = time.perf_counter()
            manager = DownloadManager() 

            for i, track in enumerate(self.tracks):
                if track.downloaded: 
                    manager.complete_download(track.id, "Already downloaded", 0.0)
                    continue

                update_progress(f"\n[{i + 1}/{total_tracks}] Starting download: {track.title} - {track.artists}")
                
                manager.start_download(track.id)

                track_outpath = self.outpath
                if self.is_playlist:
                    if self.use_artist_subfolders:
                        artist_folder = re.sub(r'[<>:"/\\|?*]', '_', track.artists.split(", ")[0])
                        track_outpath = os.path.join(track_outpath, artist_folder)
                    if self.use_album_subfolders:
                        album_folder = re.sub(r'[<>:"/\\|?*]', '_', track.album)
                        track_outpath = os.path.join(track_outpath, album_folder)
                    os.makedirs(track_outpath, exist_ok=True)

                new_filename = self.get_formatted_filename(track, i + 1)
                new_filepath = os.path.join(track_outpath, new_filename)

                if os.path.exists(new_filepath) and os.path.getsize(new_filepath) > 0:
                    update_progress(f"File already exists: {new_filename}. Skipping download.")
                    track.downloaded = True
                    size_mb = os.path.getsize(new_filepath) / (1024 * 1024)
                    manager.complete_download(track.id, new_filepath, size_mb)
                    continue

                download_success = False
                last_error = None

                for svc in self.services:
                    update_progress(f"Trying service: {svc}")
                    
                    current_ext = ".mp3" if svc == "youtube" else ".m4a" if svc == "amazon" else ".flac"
                    
                    new_filename = self.get_formatted_filename(track, i + 1, current_ext)
                    new_filepath = os.path.join(track_outpath, new_filename)
                    
                    if svc == "tidal": downloader = TidalDownloader()
                    elif svc == "deezer": downloader = DeezerDownloader()
                    elif svc == "qobuz": downloader = QobuzDownloader()
                    elif svc == "amazon": downloader = AmazonDownloader()
                    elif svc == "youtube": downloader = YouTubeDownloader()
                    else: downloader = TidalDownloader()

                    progress_cb = RichProgressCallback(item_id=track.id)
                    if hasattr(downloader, "set_progress_callback"):
                        downloader.set_progress_callback(progress_cb)

                    try:
                        downloaded_file = None
                        
                        if svc == "tidal":
                            downloaded_file = downloader.download_by_spotify_id(
                                spotify_track_id=track.id,
                                isrc=track.isrc,
                                output_dir=track_outpath,
                                filename_format=self.filename_format,
                                include_track_number=self.use_track_numbers,
                                position=track.track_number or i + 1,
                                spotify_track_name=track.title,
                                spotify_artist_name=track.artists,
                                spotify_album_name=track.album,
                                spotify_album_artist=track.album_artist,
                                spotify_release_date=track.release_date,
                                use_album_track_number=self.use_track_numbers,
                                spotify_cover_url=track.cover_url
                            )
                        elif svc == "deezer":
                            if not track.isrc: raise Exception("No ISRC for Deezer")
                            ok = asyncio.run(downloader.download_by_isrc(track.isrc, track_outpath))
                            if not ok: raise Exception("Deezer download failed")
                            import glob
                            flac_files = glob.glob(os.path.join(track_outpath, "*.flac"))
                            if flac_files: downloaded_file = max(flac_files, key=os.path.getctime)
                        elif svc == "qobuz":
                            if not track.isrc: raise Exception("No ISRC for Qobuz")
                            downloaded_file = downloader.download_by_isrc(
                                isrc=track.isrc,
                                output_dir=track_outpath,
                                quality="6",
                                filename_format=self.filename_format.replace("{title}", "temp_qobuz").replace("{artist}", "temp"),
                                include_track_number=False,
                                position=track.track_number or i + 1,
                                spotify_track_name=track.title,
                                spotify_artist_name=track.artists,
                                spotify_album_name=track.album,
                                spotify_album_artist=track.album_artist,
                                spotify_release_date=track.release_date, 
                                use_album_track_number=self.use_track_numbers,
                                spotify_cover_url=track.cover_url
                            )
                        elif svc == "amazon":
                            downloaded_file = downloader.download_by_spotify_id(
                                spotify_track_id=track.id,
                                output_dir=track_outpath,
                                isrc=track.isrc,
                                filename_format="temp_amazon",
                                include_track_number=self.use_track_numbers,
                                position=track.track_number or i + 1,
                                spotify_track_name=track.title,
                                spotify_artist_name=track.artists,
                                spotify_album_name=track.album,
                                spotify_album_artist=track.album_artist, 
                                spotify_release_date=track.release_date, 
                                use_album_track_number=self.use_track_numbers,
                                spotify_cover_url=track.cover_url
                            )
                        elif svc == "youtube":
                            downloaded_file = downloader.download_by_spotify_id(
                                spotify_track_id=track.id,
                                output_dir=track_outpath,
                                spotify_track_name=track.title,
                                spotify_artist_name=track.artists,
                                spotify_album_name=track.album,
                                spotify_album_artist=track.album_artist, 
                                spotify_release_date=track.release_date, 
                                spotify_track_number=track.track_number or i + 1,
                                spotify_total_tracks=len(self.tracks),
                                spotify_disc_number=1,
                                spotify_total_discs=1,
                                spotify_cover_url=track.cover_url
                            )

                        if downloaded_file and os.path.exists(downloaded_file):
                            if downloaded_file != new_filepath:
                                try:
                                    if os.path.exists(new_filepath): os.remove(new_filepath)
                                    shutil.move(downloaded_file, new_filepath)
                                except OSError as e:
                                    print() 
                                    update_progress(f"[!] Rename/Move failed: {e}")
                            
                            print()
                            update_progress(f"Successfully downloaded using: {svc}")
                            track.downloaded = True
                            download_success = True
                            
                            final_size = os.path.getsize(new_filepath) / (1024 * 1024)
                            manager.complete_download(track.id, new_filepath, final_size)
                            
                            break
                        else:
                            raise Exception("File missing after download")

                    except Exception as e:
                        last_error = str(e)
                        print()
                        update_progress(f"[X] {svc} failed: {e}")
                        continue

                if not download_success:
                    self.failed_tracks.append((track.title, track.artists, last_error))
                    print()
                    update_progress(f"[X] Failed all services")
                    manager.fail_download(track.id, last_error or "All services failed")

                if i < total_tracks - 1:
                    time.sleep(1.5)

            total_elapsed = time.perf_counter() - start
            on_download_finished(True, "Download completed!", self.failed_tracks, total_elapsed)

        except Exception as e:
            on_download_finished(False, str(e), self.failed_tracks)


def parse_args():
    parser = argparse.ArgumentParser(
        prog="spotiflac",
        usage="spotiflac url output_dir [-h] [--service {tidal,deezer,qobuz,amazon} ...] "
              "[--filename-format FILENAME_FORMAT] [--use-track-numbers] "
              "[--use-artist-subfolders] [--use-album-subfolders] [--loop LOOP]"
    )

    parser.add_argument("url", help="Spotify URL")
    parser.add_argument("output_dir", help="Output directory")
    parser.add_argument("--service", choices=["tidal", "deezer", "qobuz", "amazon", "youtube"], nargs="+", default=["tidal"])
    parser.add_argument("--filename-format", default="{title} - {artist}")
    parser.add_argument("--use-track-numbers", action="store_true")
    parser.add_argument("--use-artist-subfolders", action="store_true")
    parser.add_argument("--use-album-subfolders", action="store_true")
    parser.add_argument("--loop", type=int, help="Loop delay in minutes")

    return parser.parse_args()


def SpotiFLAC(url, output_dir, services=["tidal"], filename_format="{title} - {artist}", use_track_numbers=False, use_artist_subfolders=False, use_album_subfolders=False, loop=None):
    global config
    config = Config(url, output_dir, services, filename_format, use_track_numbers, use_artist_subfolders, use_album_subfolders, False, False, False, "", [], None, loop)
    try:
        fetch_tracks(config.url)
        download_tracks(range(len(config.tracks)))
    except KeyboardInterrupt:
        print("\n\n[!] Download stopped by user. Partial files were cleaned up.")


def main():
    args = parse_args()
    SpotiFLAC(args.url, args.output_dir, args.service, args.filename_format, args.use_track_numbers, args.use_artist_subfolders, args.use_album_subfolders, args.loop)


if __name__ == "__main__":
    main()