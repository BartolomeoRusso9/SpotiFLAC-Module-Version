"""
Progress tracking thread-safe.
Mantenuto compatibile con il codice originale ma con lock più granulari
e tipo hints completi.
"""
from __future__ import annotations
import threading
import time
from dataclasses import dataclass, field
from enum import Enum


class DownloadStatus(Enum):
    QUEUED      = "queued"
    DOWNLOADING = "downloading"
    COMPLETED   = "completed"
    FAILED      = "failed"
    SKIPPED     = "skipped"


@dataclass
class DownloadItem:
    id:            str
    track_name:    str
    artist_name:   str
    album_name:    str
    spotify_id:    str
    status:        DownloadStatus = DownloadStatus.QUEUED
    progress:      float          = 0.0
    total_size:    float          = 0.0
    speed:         float          = 0.0
    start_time:    float          = 0.0
    end_time:      float          = 0.0
    error_message: str            = ""
    file_path:     str            = ""


class DownloadManager:
    """
    Singleton thread-safe per lo stato globale dei download.
    Ispirato al pattern sync.RWMutex del Go.
    """
    _instance: "DownloadManager | None" = None
    _creation_lock = threading.Lock()

    def __new__(cls) -> "DownloadManager":
        with cls._creation_lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._init_state()
                cls._instance = inst
        return cls._instance

    def _init_state(self) -> None:
        self._lock            = threading.RLock()
        self._queue:    list[DownloadItem] = []
        self.is_downloading   = False
        self.current_speed    = 0.0
        self.total_downloaded = 0.0
        self.current_item_id  = ""
        self.session_start    = 0.0

    # ------------------------------------------------------------------

    def add_to_queue(
        self,
        item_id:     str,
        track_name:  str,
        artist_name: str,
        album_name:  str,
        spotify_id:  str,
    ) -> None:
        with self._lock:
            self._queue.append(DownloadItem(
                id=item_id, track_name=track_name,
                artist_name=artist_name, album_name=album_name,
                spotify_id=spotify_id,
            ))
            if self.session_start == 0.0:
                self.session_start = time.time()

    def start_download(self, item_id: str) -> None:
        with self._lock:
            for item in self._queue:
                if item.id == item_id:
                    item.status     = DownloadStatus.DOWNLOADING
                    item.start_time = time.time()
                    item.progress   = 0.0
                    break
            self.current_item_id = item_id
            self.is_downloading  = True

    def update_progress(self, item_id: str, progress_mb: float, speed_mbps: float) -> None:
        with self._lock:
            self.current_speed = speed_mbps
            for item in self._queue:
                if item.id == item_id:
                    item.progress = progress_mb
                    item.speed    = speed_mbps
                    break

    def complete_download(self, item_id: str, filepath: str, final_size_mb: float) -> None:
        with self._lock:
            for item in self._queue:
                if item.id == item_id:
                    item.status     = DownloadStatus.COMPLETED
                    item.end_time   = time.time()
                    item.file_path  = filepath
                    item.progress   = final_size_mb
                    item.total_size = final_size_mb
                    self.total_downloaded += final_size_mb
                    break
            self.is_downloading = False

    def fail_download(self, item_id: str, error_msg: str) -> None:
        with self._lock:
            for item in self._queue:
                if item.id == item_id:
                    item.status        = DownloadStatus.FAILED
                    item.end_time      = time.time()
                    item.error_message = error_msg
                    break
            self.is_downloading = False

    def get_stats(self) -> dict:
        with self._lock:
            counts = {s: 0 for s in DownloadStatus}
            for item in self._queue:
                counts[item.status] += 1
            return {
                "is_downloading":   self.is_downloading,
                "current_speed":    self.current_speed,
                "total_downloaded": self.total_downloaded,
                "queued":           counts[DownloadStatus.QUEUED],
                "completed":        counts[DownloadStatus.COMPLETED],
                "failed":           counts[DownloadStatus.FAILED],
                "skipped":          counts[DownloadStatus.SKIPPED],
                "queue":            [vars(i) for i in self._queue],
            }


class ProgressCallback:
    """
    Callback progresso con throttle a 250 ms per evitare flickering.
    Compatibile con la signature (current_bytes, total_bytes).
    """

    def __init__(self, item_id: str = "") -> None:
        self._item_id   = item_id
        self._start     = time.time()
        self._last_time = self._start
        self._last_bytes = 0
        self._manager   = DownloadManager()

    def __call__(self, current_bytes: int, total_bytes: int) -> None:
        now       = time.time()
        time_diff = now - self._last_time

        if time_diff < 0.25 and current_bytes != total_bytes:
            return

        bytes_diff = current_bytes - self._last_bytes
        speed_bps  = bytes_diff / time_diff if time_diff > 0 else 0.0
        speed_mbs  = speed_bps / (1024 * 1024)
        mb_done    = current_bytes / (1024 * 1024)

        if total_bytes > 0:
            pct     = current_bytes / total_bytes
            filled  = int(pct * 20)
            bar     = "█" * filled + "░" * (20 - filled)
            eta_s   = (total_bytes - current_bytes) / speed_bps if speed_bps > 0 else 0
            eta_str = _fmt_eta(eta_s) if eta_s > 0 else "--:--"
            mb_tot  = total_bytes / (1024 * 1024)
            print(
                f"\r  [{bar}] {pct*100:5.1f}%  "
                f"{mb_done:.1f}/{mb_tot:.1f} MB  "
                f"{speed_mbs:.2f} MB/s  ETA {eta_str}   ",
                end="", flush=True,
            )
        else:
            print(
                f"\r  ↓ {mb_done:.2f} MB  ·  {speed_mbs:.2f} MB/s   ",
                end="", flush=True,
            )

        if self._item_id:
            self._manager.update_progress(self._item_id, mb_done, speed_mbs)

        self._last_time  = now
        self._last_bytes = current_bytes

        if current_bytes == total_bytes and total_bytes > 0:
            elapsed = now - self._start
            mb_tot  = total_bytes / (1024 * 1024)
            avg_mbs = mb_tot / elapsed if elapsed > 0 else 0
            print(f"\r  ✓ {mb_tot:.2f} MB scaricati in {_fmt_eta(elapsed)}  ·  media {avg_mbs:.2f} MB/s   ")


def _fmt_eta(seconds: float) -> str:
    s = int(seconds)
    m, s = divmod(s, 60)
    return f"{m:02d}:{s:02d}"


# Alias retrocompatibile
RichProgressCallback = ProgressCallback
