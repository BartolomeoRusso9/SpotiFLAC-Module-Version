import time
import threading
from enum import Enum
from dataclasses import dataclass

class DownloadStatus(Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"

@dataclass
class DownloadItem:
    id: str
    track_name: str
    artist_name: str
    album_name: str
    spotify_id: str
    status: DownloadStatus = DownloadStatus.QUEUED
    progress: float = 0.0
    total_size: float = 0.0
    speed: float = 0.0
    start_time: float = 0.0
    end_time: float = 0.0
    error_message: str = ""
    file_path: str = ""

class DownloadManager:
    """
    Equivalente ao estado global com Mutex (sync.RWMutex) do Go.
    Usamos o padrão Singleton para manter o estado global em todo o módulo.
    """
    _instance = None
    _creation_lock = threading.Lock()

    def __new__(cls):
        with cls._creation_lock:
            if cls._instance is None:
                cls._instance = super(DownloadManager, cls).__new__(cls)
                cls._instance._init_state()
        return cls._instance

    def _init_state(self):
        self.lock = threading.RLock()
        self.queue: list[DownloadItem] = []
        self.is_downloading = False
        self.current_speed = 0.0
        self.total_downloaded = 0.0
        self.current_item_id = ""
        self.session_start_time = 0.0

    def add_to_queue(self, item_id, track_name, artist_name, album_name, spotify_id):
        with self.lock:
            item = DownloadItem(
                id=item_id,
                track_name=track_name,
                artist_name=artist_name,
                album_name=album_name,
                spotify_id=spotify_id
            )
            self.queue.append(item)
            if self.session_start_time == 0.0:
                self.session_start_time = time.time()

    def start_download(self, item_id):
        with self.lock:
            for item in self.queue:
                if item.id == item_id:
                    item.status = DownloadStatus.DOWNLOADING
                    item.start_time = time.time()
                    item.progress = 0.0
                    break
            self.current_item_id = item_id
            self.is_downloading = True

    def update_progress(self, item_id, progress_mb, speed_mbps):
        with self.lock:
            self.current_speed = speed_mbps
            for item in self.queue:
                if item.id == item_id:
                    item.progress = progress_mb
                    item.speed = speed_mbps
                    break

    def complete_download(self, item_id, filepath, final_size_mb):
        with self.lock:
            for item in self.queue:
                if item.id == item_id:
                    item.status = DownloadStatus.COMPLETED
                    item.end_time = time.time()
                    item.file_path = filepath
                    item.progress = final_size_mb
                    item.total_size = final_size_mb
                    
                    self.total_downloaded += final_size_mb
                    break
            self.is_downloading = False

    def fail_download(self, item_id, error_msg):
        with self.lock:
            for item in self.queue:
                if item.id == item_id:
                    item.status = DownloadStatus.FAILED
                    item.end_time = time.time()
                    item.error_message = error_msg
                    break
            self.is_downloading = False

    def get_queue_info(self) -> dict:
        with self.lock:
            queued = completed = failed = skipped = 0
            for item in self.queue:
                if item.status == DownloadStatus.QUEUED: queued += 1
                elif item.status == DownloadStatus.COMPLETED: completed += 1
                elif item.status == DownloadStatus.FAILED: failed += 1
                elif item.status == DownloadStatus.SKIPPED: skipped += 1

            return {
                "is_downloading": self.is_downloading,
                "current_speed": self.current_speed,
                "total_downloaded": self.total_downloaded,
                "queued_count": queued,
                "completed_count": completed,
                "failed_count": failed,
                "skipped_count": skipped,
                "queue": [vars(item) for item in self.queue]
            }

class RichProgressCallback:
    def __init__(self, item_id: str = ""):
        self.item_id = item_id
        self.start_time = time.time()
        self.last_time = self.start_time
        self.last_bytes = 0
        self.manager = DownloadManager()

    def __call__(self, current_bytes: int, total_bytes: int) -> None:
        now = time.time()
        time_diff = now - self.last_time
        
        # Atualiza a tela a cada 0.25s para não piscar (flickering)
        if time_diff >= 0.25 or current_bytes == total_bytes:
            bytes_diff = current_bytes - self.last_bytes
            
            # Calcula MB/s
            speed_mbps = 0.0
            if time_diff > 0:
                speed_mbps = (bytes_diff / (1024 * 1024)) / time_diff
                
            mb_downloaded = current_bytes / (1024 * 1024)

            # Print visual no terminal
            if total_bytes > 0:
                percent = (current_bytes / total_bytes) * 100
                print(f"\rDownloaded: {percent:.1f}% | {mb_downloaded:.2f} MB | {speed_mbps:.2f} MB/s", end="", flush=True)
            else:
                print(f"\rDownloaded: {mb_downloaded:.2f} MB | {speed_mbps:.2f} MB/s", end="", flush=True)

            # Atualiza o estado global
            if self.item_id:
                self.manager.update_progress(self.item_id, mb_downloaded, speed_mbps)

            # Reseta os contadores para o próximo ciclo
            self.last_time = now
            self.last_bytes = current_bytes

        if current_bytes == total_bytes and total_bytes > 0:
            print()