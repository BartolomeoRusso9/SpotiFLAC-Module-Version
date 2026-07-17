"""
progress.py — Async-native progress tracking
"""

from __future__ import annotations

import asyncio
import io
import logging
import sys
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from tqdm import tqdm

# tqdm.get_lock() rimane come lock nativo di tqdm (non è un nostro accrocchio,
# è la sincronizzazione interna della libreria per la scrittura su stderr).


def safe_print(*args: object, **kwargs: Any) -> None:
    content = " ".join(str(a) for a in args)
    with tqdm.get_lock():
        tqdm.write(content, file=kwargs.get("file", sys.stdout))


def safe_tqdm_write(msg: str, file: io.TextIOBase | None = None) -> None:
    with tqdm.get_lock():
        tqdm.write(msg, file=file or sys.stdout)


class TqdmLoggingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self._message_cache: dict[str, float] = {}
        self._cache_ttl = 0.5

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            now = time.time()
            if msg in self._message_cache and now - self._message_cache[msg] < self._cache_ttl:
                return
            self._message_cache[msg] = now
            self._message_cache = {
                k: v for k, v in self._message_cache.items() if now - v < self._cache_ttl * 2
            }
            with tqdm.get_lock():
                tqdm.write(msg, file=sys.stderr)
        except Exception:
            self.handleError(record)


class _TqdmTextIOProxy(io.TextIOBase):
    def __init__(self, original: io.TextIOBase) -> None:
        self._original = original
        self._buf = ""

    def write(self, s: str) -> int:
        with tqdm.get_lock():
            s = s.replace("\r", "")
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                tqdm.write(line, file=self._original)
        return len(s)

    def flush(self) -> None:
        with tqdm.get_lock():
            if self._buf:
                tqdm.write(self._buf, file=self._original)
                self._buf = ""
            try:
                self._original.flush()
            except Exception:
                pass

    @property
    def encoding(self) -> str:
        return getattr(self._original, "encoding", "utf-8")

    def fileno(self) -> int:
        return self._original.fileno()

    def isatty(self) -> bool:
        return getattr(self._original, "isatty", lambda: False)()


def install_console_interception() -> None:
    if not isinstance(sys.stdout, _TqdmTextIOProxy):
        sys.stdout = _TqdmTextIOProxy(sys.__stdout__)
    if not isinstance(sys.stderr, _TqdmTextIOProxy):
        sys.stderr = _TqdmTextIOProxy(sys.__stderr__)

    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, logging.StreamHandler):
            root.removeHandler(handler)

    for name, logger_obj in list(logging.Logger.manager.loggerDict.items()):
        if isinstance(logger_obj, logging.Logger) and (
            name == "SpotiFLAC" or name.startswith("SpotiFLAC.")
        ):
            for handler in list(logger_obj.handlers):
                if isinstance(handler, logging.StreamHandler):
                    logger_obj.removeHandler(handler)
            logger_obj.propagate = True

    new_handler = TqdmLoggingHandler()
    new_handler.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
    new_handler.setLevel(logging.getLogger().level or logging.WARNING)
    logging.getLogger().addHandler(new_handler)


def uninstall_console_interception() -> None:
    if isinstance(sys.stdout, _TqdmTextIOProxy):
        sys.stdout = sys.__stdout__
    if isinstance(sys.stderr, _TqdmTextIOProxy):
        sys.stderr = sys.__stderr__


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


class DownloadBroadcaster:
    """
    Singleton per lo streaming degli eventi di progresso verso eventuali
    listener esterni (es. la GUI webview). Prima usava _CrossLoopLock per
    proteggere l'accesso a self._listeners da più thread/loop paralleli.
    Con un unico event loop, asyncio.Lock() è sufficiente e corretto.
    """

    _instance: "DownloadBroadcaster | None" = None

    def __new__(cls) -> "DownloadBroadcaster":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._listeners = set()
            cls._instance._lock = asyncio.Lock()
            cls._instance._last_broadcast_time = 0.0
        return cls._instance

    async def subscribe(self, queue: asyncio.Queue) -> None:
        async with self._lock:
            self._listeners.add(queue)

    async def unsubscribe(self, queue: asyncio.Queue) -> None:
        async with self._lock:
            self._listeners.discard(queue)

    async def broadcast_immediate(self, event_data: dict) -> None:
        self._last_broadcast_time = time.time()
        await self._send_to_all(event_data)

    async def broadcast_progress(self, event_data: dict) -> None:
        now = time.time()
        if now - self._last_broadcast_time >= 0.25:
            self._last_broadcast_time = now
            await self._send_to_all(event_data)

    async def _send_to_all(self, event_data: dict) -> None:
        async with self._lock:
            for q in list(self._listeners):
                try:
                    q.put_nowait(event_data)
                except Exception:
                    pass


class DownloadManager:
    """
    Singleton che tiene lo stato della coda di download.
    _CrossLoopLock -> asyncio.Lock(): valido perché non esistono più event
    loop multipli concorrenti (nessun thread fa più il proprio asyncio.run()).
    """

    _instance: "DownloadManager | None" = None

    def __new__(cls) -> "DownloadManager":
        if cls._instance is None:
            inst = super().__new__(cls)
            inst._init_state()
            cls._instance = inst
        return cls._instance

    def _init_state(self) -> None:
        self._lock = asyncio.Lock()
        self._queue: list[DownloadItem] = []
        self.total_downloaded = 0.0
        self.current_item_id = ""
        self.session_start = 0.0

    async def add_to_queue(
        self,
        item_id: str,
        track_name: str,
        artist_name: str,
        album_name: str,
        spotify_id: str,
    ) -> None:
        async with self._lock:
            self._queue.append(
                DownloadItem(
                    id=item_id,
                    track_name=track_name,
                    artist_name=artist_name,
                    album_name=album_name,
                    spotify_id=spotify_id,
                )
            )
            if self.session_start == 0.0:
                self.session_start = time.time()

        stats = await self.get_stats()
        await DownloadBroadcaster().broadcast_immediate(stats)

    async def start_download(self, item_id: str) -> None:
        async with self._lock:
            for item in self._queue:
                if item.id == item_id:
                    item.status, item.start_time, item.progress = (
                        DownloadStatus.DOWNLOADING,
                        time.time(),
                        0.0,
                    )
                    break
            self.current_item_id = item_id

        stats = await self.get_stats()
        await DownloadBroadcaster().broadcast_immediate(stats)

    async def update_progress(
        self, item_id: str, progress_mb: float, total_mb: float, speed_mbps: float
    ) -> None:
        async with self._lock:
            for item in self._queue:
                if item.id == item_id:
                    item.progress = progress_mb
                    if total_mb > 0:
                        item.total_size = total_mb
                    item.speed = speed_mbps
                    break

        stats = await self.get_stats()
        await DownloadBroadcaster().broadcast_progress(stats)

    async def complete_download(
        self, item_id: str, filepath: str, final_size_mb: float
    ) -> None:
        async with self._lock:
            for item in self._queue:
                if item.id == item_id:
                    item.status, item.end_time, item.file_path = (
                        DownloadStatus.COMPLETED,
                        time.time(),
                        filepath,
                    )
                    item.progress, item.total_size = final_size_mb, final_size_mb
                    self.total_downloaded += final_size_mb
                    break

        stats = await self.get_stats()
        await DownloadBroadcaster().broadcast_immediate(stats)

    async def fail_download(self, item_id: str, error_msg: str) -> None:
        async with self._lock:
            for item in self._queue:
                if item.id == item_id:
                    item.status, item.end_time, item.error_message = (
                        DownloadStatus.FAILED,
                        time.time(),
                        error_msg,
                    )
                    break

        stats = await self.get_stats()
        await DownloadBroadcaster().broadcast_immediate(stats)

    async def skip_download(self, item_id: str) -> None:
        async with self._lock:
            for item in self._queue:
                if item.id == item_id:
                    item.status, item.end_time = DownloadStatus.SKIPPED, time.time()
                    break

        stats = await self.get_stats()
        await DownloadBroadcaster().broadcast_immediate(stats)

    async def get_item_speed(self, item_id: str) -> float:
        async with self._lock:
            for item in self._queue:
                if item.id == item_id:
                    return item.speed
        return 0.0

    async def get_stats(self) -> dict:
        async with self._lock:
            queue_items = []
            completed_items = []

            is_downloading = False
            current_speed = 0.0
            active_progress = 0.0
            queued = 0
            failed = 0
            skipped = 0

            for i in self._queue:
                item_data = {
                    "id": i.id,
                    "track_name": i.track_name,
                    "artist_name": i.artist_name,
                    "album_name": i.album_name,
                    "spotify_id": i.spotify_id,
                    "status": i.status.value,
                    "progress": i.progress,
                    "total_size": i.total_size,
                    "speed": i.speed,
                    "file_path": i.file_path,
                    "end_time": i.end_time,
                    "error_message": i.error_message,
                }
                queue_items.append(item_data)

                if i.status == DownloadStatus.DOWNLOADING:
                    is_downloading = True
                    current_speed += i.speed
                    active_progress += i.progress
                elif i.status == DownloadStatus.QUEUED:
                    queued += 1
                elif i.status == DownloadStatus.COMPLETED:
                    completed_items.append(item_data)
                elif i.status == DownloadStatus.FAILED:
                    failed += 1
                elif i.status == DownloadStatus.SKIPPED:
                    skipped += 1

            completed_items.sort(key=lambda x: x["end_time"], reverse=True)
            latest_completed = completed_items[:20]

            return {
                "is_downloading": is_downloading,
                "current_speed": current_speed,
                "total_downloaded": self.total_downloaded + active_progress,
                "queued": queued,
                "completed": len(completed_items),
                "failed": failed,
                "skipped": skipped,
                "downloads": queue_items,
                "queue": queue_items,
                "latest_completed": latest_completed,
            }

    async def reset(self) -> None:
        async with self._lock:
            self._queue = []
            self.total_downloaded = 0.0
            self.current_item_id = ""
            self.session_start = 0.0

        stats = await self.get_stats()
        await DownloadBroadcaster().broadcast_immediate(stats)


class ProgressManager:
    """
    Gestisce le barre tqdm per-track.

    Prima: queue.Queue (thread-safe nativa) + threading.Thread consumer,
    necessari perché ogni download girava nel proprio thread/event loop.

    Ora: asyncio.Queue() + asyncio.Task consumer. Un solo event loop, quindi
    niente più bisogno di primitive thread-safe: enqueue_progress() è una
    semplice put_nowait() sulla queue asyncio (safe da qualunque coroutine
    o callback sincrono chiamato all'interno dello stesso loop).
    """

    _bars: dict[str, tqdm] = {}
    _slot_map: dict[str, int] = {}
    _master_bar: tqdm | None = None
    _master_enabled: bool = False

    _event_queue: "asyncio.Queue | None" = None
    _worker_task: "asyncio.Task | None" = None

    # ------------------------------------------------------------------
    # Worker lifecycle
    # ------------------------------------------------------------------

    @classmethod
    def start_worker(cls) -> None:
        """
        Avvia il task consumer in modo idempotente. Deve essere chiamato da
        dentro l'event loop attivo (create_task lo richiede).
        """
        if cls._event_queue is None:
            cls._event_queue = asyncio.Queue()

        if cls._worker_task is not None and not cls._worker_task.done():
            return

        cls._worker_task = asyncio.create_task(cls._process_events_async())

    @classmethod
    async def stop_worker(cls) -> None:
        if cls._worker_task is not None:
            if cls._event_queue is not None:
                # sblocca il .get() in attesa nel task consumer
                await cls._event_queue.put(None)
            try:
                await asyncio.wait_for(cls._worker_task, timeout=5)
            except asyncio.TimeoutError:
                cls._worker_task.cancel()
            cls._worker_task = None

    @classmethod
    async def _process_events_async(cls) -> None:
        assert cls._event_queue is not None
        while True:
            event = await cls._event_queue.get()
            if event is None:
                break

            item_id, track_name, current_bytes, total_bytes = event

            try:
                with tqdm.get_lock():
                    bar = cls._bars.get(item_id)
                    if bar is None:
                        bar = cls.create_bar(item_id, track_name, total_bytes)

                    if total_bytes != bar.total:
                        bar.total = total_bytes

                    if current_bytes < bar.n:
                        bar.reset(total=total_bytes)
                        bar.update(current_bytes)
                    else:
                        delta = current_bytes - bar.n
                        if delta > 0:
                            bar.update(delta)

                    if total_bytes is not None and current_bytes >= total_bytes:
                        cls.release_bar(item_id)
            except Exception:
                logging.getLogger(__name__).exception("ProgressManager consumer crashed")

    @classmethod
    def enqueue_progress(
        cls, item_id: str, track_name: str, current_bytes: int, total_bytes: int | None
    ) -> None:
        """
        Chiamata da coroutine/callback dentro il loop principale. Va avviato
        il worker lazy la prima volta (idempotente) e poi si fa una semplice
        put_nowait() — nessuna primitiva thread-safe necessaria.
        """
        cls.start_worker()
        if cls._event_queue is not None:
            try:
                cls._event_queue.put_nowait(
                    (item_id, track_name, current_bytes, total_bytes)
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Bar management (sync, invariato — protetto da tqdm.get_lock())
    # ------------------------------------------------------------------

    @classmethod
    def _allocate_slot(cls, item_id: str) -> int:
        if item_id in cls._slot_map:
            return cls._slot_map[item_id]
        used_slots = set(cls._slot_map.values())
        slot = 0
        while slot in used_slots:
            slot += 1
        cls._slot_map[item_id] = slot
        return slot

    @classmethod
    def get_effective_position(cls, slot: int) -> int:
        return slot + (1 if cls._master_enabled else 0)

    @classmethod
    def create_bar(cls, item_id: str, track_name: str, total_bytes: int | None) -> tqdm:
        if item_id in cls._bars:
            return cls._bars[item_id]

        slot = cls._allocate_slot(item_id)
        display_name = track_name.strip()
        if len(display_name) > 18:
            display_name = display_name[:15] + "..."

        bar = tqdm(
            total=total_bytes if total_bytes and total_bytes > 0 else None,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc=f"Track: {display_name:<18}",
            leave=False,
            position=cls.get_effective_position(slot),
            dynamic_ncols=True,
            miniters=1,
            smoothing=0.2,
            file=sys.__stderr__,
        )

        cls._bars[item_id] = bar
        return bar

    @classmethod
    def release_bar(cls, item_id: str) -> None:
        bar = cls._bars.pop(item_id, None)
        if bar is None:
            cls._slot_map.pop(item_id, None)
            return
        try:
            bar.clear()
            bar.close()
        except Exception:
            pass
        cls._slot_map.pop(item_id, None)

    @classmethod
    def clear_item(cls, item_id: str) -> None:
        with tqdm.get_lock():
            cls.release_bar(item_id)

    @classmethod
    async def clear_all(cls) -> None:
        await cls.stop_worker()
        with tqdm.get_lock():
            for item_id in list(cls._bars):
                cls.release_bar(item_id)
            cls._slot_map.clear()
            cls.clear_master_bar()

    @classmethod
    def initialize_master_bar(
        cls, total_items: int, description: str = "Progress", at_top: bool = True
    ) -> None:
        if not at_top:
            raise ValueError(
                "Only top-aligned master bar is supported by ProgressManager at this time."
            )
        with tqdm.get_lock():
            cls.clear_master_bar()
            cls._master_enabled = True
            cls._master_bar = tqdm(
                total=total_items,
                desc=description,
                leave=True,
                position=0,
                dynamic_ncols=True,
                miniters=1,
                file=sys.__stderr__,
            )

    @classmethod
    def clear_master_bar(cls) -> None:
        with tqdm.get_lock():
            if cls._master_bar is None:
                cls._master_enabled = False
                return
            try:
                cls._master_bar.clear()
                cls._master_bar.close()
            except Exception:
                pass
            cls._master_bar = None
            cls._master_enabled = False

    @classmethod
    def increment_master(cls, step: int = 1) -> None:
        with tqdm.get_lock():
            if cls._master_bar is None:
                return
            cls._master_bar.update(step)
            cls._master_bar.refresh()

    @classmethod
    def reset_master_total(cls, total_items: int) -> None:
        with tqdm.get_lock():
            if cls._master_bar is None:
                return
            cls._master_bar.reset(total=total_items)
            cls._master_bar.refresh()


class ProgressCallback:
    """
    Callback di progresso passato ai provider. Invariato nella logica di
    throttling; enqueue_progress() ora è thread-agnostic per costruzione
    (asyncio.Queue), quindi resta sincrona e non-bloccante come prima.
    """

    def __init__(self, item_id: str = "", track_name: str = "") -> None:
        self._item_id = item_id
        self._track_name = track_name
        self._last_refresh_time = 0.0
        self._last_reported_bytes = 0

    async def __call__(self, current_bytes: int, total_bytes: int) -> None:
        now = time.time()
        is_final = bool(total_bytes and current_bytes >= total_bytes)

        if (
            not is_final
            and self._last_refresh_time > 0
            and (now - self._last_refresh_time) < 0.1
        ):
            return

        current_bytes = max(0, current_bytes)
        total_bytes = total_bytes if total_bytes > 0 else None

        ProgressManager.enqueue_progress(
            self._item_id, self._track_name, current_bytes, total_bytes
        )
        current_mb = current_bytes / (1024 * 1024)
        total_mb = total_bytes / (1024 * 1024) if total_bytes else 0.0

        if self._last_refresh_time == 0.0:
            self._last_refresh_time = now
            self._last_reported_bytes = current_bytes
            speed_mbps = 0.0
        else:
            time_diff = now - self._last_refresh_time
            bytes_diff = current_bytes - self._last_reported_bytes
            speed_mbps = (bytes_diff / (1024 * 1024)) / time_diff
            self._last_refresh_time = now
            self._last_reported_bytes = current_bytes

        asyncio.create_task(
            DownloadManager().update_progress(
                self._item_id, current_mb, total_mb, speed_mbps
            )
        )

    @classmethod
    def clear_item(cls, item_id: str) -> None:
        ProgressManager.clear_item(item_id)


RichProgressCallback = ProgressCallback