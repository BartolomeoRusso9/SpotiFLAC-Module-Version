"""progress.py — Async-native progress tracking."""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import sys
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from tqdm import tqdm
from typing_extensions import Self

from .console import print_track_progress
from .output_sink import STDERR, STDOUT, emit, sink_active

# tqdm.get_lock() remains the native tqdm lock (this is not our own hack;
# it is the library's internal synchronization for writing to stderr).


# A tqdm bar is a stream of carriage returns: on a terminal that draws a
# moving bar, but in a log it is unreadable escape soup — `docker logs` and
# most log viewers collapse each refresh into a "[285B blob data]" line, which
# buries the messages that actually say what happened. Bars are therefore
# drawn only on a real terminal; everywhere else they are replaced by the
# throttled textual progress below.
_PROGRESS_LOG_PERCENT_STEP = 25
_PROGRESS_LOG_MIN_INTERVAL_S = 10.0


def progress_bars_enabled() -> bool:
    """Whether animated tqdm bars may be drawn.

    An installed output sink settles it first and without appeal: a bar
    writes carriage returns straight to the real stderr, so drawing one
    underneath a UI that owns the screen tears the frame no matter where the
    surrounding lines were routed. ``SPOTIFLAC_PROGRESS_BARS`` forces the
    answer either way below that (useful to keep the bars when piping to a
    terminal-aware pager, or to drop them while debugging locally);
    otherwise bars are drawn only when stderr is an interactive terminal.
    """
    if sink_active():
        return False
    forced = os.getenv("SPOTIFLAC_PROGRESS_BARS")
    if forced is not None:
        return forced.strip().lower() in {"1", "true", "yes", "on"}
    try:
        return bool(sys.__stderr__) and sys.__stderr__.isatty()
    except Exception:
        return False


def safe_print(*args: object, **kwargs: Any) -> None:
    content = " ".join(str(a) for a in args)
    emit(content, stream=STDOUT, file=kwargs.get("file"))


def safe_tqdm_write(msg: str, file: io.TextIOBase | None = None) -> None:
    emit(msg, stream=STDOUT, file=file)


class TqdmLoggingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self._message_cache: dict[str, float] = {}
        self._cache_ttl = 0.5

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            now = time.time()
            if (
                msg in self._message_cache
                and now - self._message_cache[msg] < self._cache_ttl
            ):
                return
            self._message_cache[msg] = now
            self._message_cache = {
                k: v
                for k, v in self._message_cache.items()
                if now - v < self._cache_ttl * 2
            }
            emit(msg, stream=STDERR)
        except Exception:
            self.handleError(record)


class _TqdmTextIOProxy(io.TextIOBase):
    def __init__(self, original: io.TextIOBase, stream: str = STDOUT) -> None:
        self._original = original
        self._stream = stream
        self._buf = ""

    def write(self, s: str) -> int:
        # Whole lines only, and through emit(): a bare print() anywhere in
        # the codebase is the one output path nobody remembers to route, and
        # it is the one that tears a full-screen UI.
        written = len(s)
        s = s.replace("\r", "")
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            emit(line, stream=self._stream, file=self._original)
        return written

    def flush(self) -> None:
        if self._buf:
            emit(self._buf, stream=self._stream, file=self._original)
            self._buf = ""
        with contextlib.suppress(Exception):
            self._original.flush()

    @property
    def encoding(self) -> str:
        return getattr(self._original, "encoding", "utf-8")

    def fileno(self) -> int:
        return self._original.fileno()

    def isatty(self) -> bool:
        return getattr(self._original, "isatty", lambda: False)()


# Handlers taken out by install_console_interception(), so uninstall can put
# them back: a long-lived host (the Telegram bot, the GUI) runs a download per
# job, and without this its own logging setup would be swallowed by the first
# one and a fresh tqdm handler stacked on top by every one after it.
_suspended_handlers: list[tuple[logging.Logger, logging.Handler, bool]] = []
_tqdm_handler: TqdmLoggingHandler | None = None

_DEFAULT_LOG_FORMAT = "[%(levelname)s] %(name)s: %(message)s"


def install_console_interception() -> None:
    """Routes every log record through tqdm.write so bars stay intact.

    Idempotent: calling it while already installed does nothing, instead of
    adding a second handler that would double every line.
    """
    global _tqdm_handler

    if not isinstance(sys.stdout, _TqdmTextIOProxy):
        sys.stdout = _TqdmTextIOProxy(sys.__stdout__, STDOUT)
    if not isinstance(sys.stderr, _TqdmTextIOProxy):
        sys.stderr = _TqdmTextIOProxy(sys.__stderr__, STDERR)

    if _tqdm_handler is not None:
        return

    root = logging.getLogger()
    targets: list[logging.Logger] = [root]
    targets.extend(
        logger_obj
        for name, logger_obj in list(logging.Logger.manager.loggerDict.items())
        if isinstance(logger_obj, logging.Logger)
        and (name == "SpotiFLAC" or name.startswith("SpotiFLAC."))
    )

    for logger_obj in targets:
        propagate = logger_obj.propagate
        for handler in list(logger_obj.handlers):
            if isinstance(handler, logging.StreamHandler):
                logger_obj.removeHandler(handler)
                _suspended_handlers.append((logger_obj, handler, propagate))
        if logger_obj is not root:
            logger_obj.propagate = True

    # Keep the host's own formatter — its timestamps are usually the only way
    # to tell when something happened once the log is read back.
    formatter = next(
        (h.formatter for _, h, _ in _suspended_handlers if h.formatter is not None),
        None,
    )
    _tqdm_handler = TqdmLoggingHandler()
    _tqdm_handler.setFormatter(formatter or logging.Formatter(_DEFAULT_LOG_FORMAT))
    _tqdm_handler.setLevel(root.level or logging.WARNING)
    root.addHandler(_tqdm_handler)


def uninstall_console_interception() -> None:
    global _tqdm_handler

    if isinstance(sys.stdout, _TqdmTextIOProxy):
        sys.stdout = sys.__stdout__
    if isinstance(sys.stderr, _TqdmTextIOProxy):
        sys.stderr = sys.__stderr__

    if _tqdm_handler is not None:
        logging.getLogger().removeHandler(_tqdm_handler)
        _tqdm_handler = None

    for logger_obj, handler, propagate in _suspended_handlers:
        logger_obj.addHandler(handler)
        logger_obj.propagate = propagate
    _suspended_handlers.clear()


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
    """Singleton for streaming progress events to external listeners
    (e.g. a GUI webview). Previously it used _CrossLoopLock to protect
    access to self._listeners from multiple parallel threads/loops.
    With a single event loop, asyncio.Lock() is sufficient and correct.
    """

    _instance: DownloadBroadcaster | None = None

    def __new__(cls) -> Self:
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
                with contextlib.suppress(Exception):
                    q.put_nowait(event_data)


class DownloadManager:
    """Singleton that holds the download queue state.
    _CrossLoopLock -> asyncio.Lock(): valid because there are no longer
    multiple concurrent event loops (no thread runs its own asyncio.run()).
    """

    _instance: DownloadManager | None = None

    def __new__(cls) -> Self:
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

    def get_stats_sync(self) -> dict:
        """Sync wrapper for callers (e.g. pywebview) that can't await.

        This one is polled on a timer while a download runs (see app.py's
        _download_stats_monitor), so the old version's "spin up a loop per
        call" was a steady drip of loop churn for the life of every download.
        """
        from .loop_runner import run_sync

        return run_sync(self.get_stats())

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
                ),
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
        self,
        item_id: str,
        progress_mb: float,
        total_mb: float,
        speed_mbps: float,
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
        self,
        item_id: str,
        filepath: str,
        final_size_mb: float,
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
    """Manages per-track tqdm bars.

    Before: queue.Queue (native thread-safe) + threading.Thread consumer,
    required because each download ran in its own thread/event loop.

    Now: asyncio.Queue() + asyncio.Task consumer. A single event loop, so
    no more need for thread-safe primitives: enqueue_progress() is a simple
    put_nowait() on the asyncio queue (safe from any coroutine or synchronous
    callback called within the same loop).
    """

    _bars: dict[str, tqdm] = {}
    _slot_map: dict[str, int] = {}
    _master_bar: tqdm | None = None
    _master_enabled: bool = False

    _event_queue: asyncio.Queue | None = None
    _worker_task: asyncio.Task | None = None
    # The loop `_event_queue` is bound to. asyncio.Queue latches onto the
    # loop that first awaits it, and this class is a process-wide singleton
    # while the GUI runs every download in its own asyncio.run() — so the
    # queue outlives the loop that owns it and has to be rebuilt per loop.
    _queue_loop: asyncio.AbstractEventLoop | None = None

    # item_id -> (last log time, last logged percentage), used only when the
    # bars are off and progress is reported as text instead.
    _progress_log_state: dict[str, tuple[float, int]] = {}

    # ------------------------------------------------------------------
    # Worker lifecycle
    # ------------------------------------------------------------------

    @classmethod
    def start_worker(cls) -> None:
        """Starts the consumer task idempotently. Must be called from
        inside the active event loop (create_task requires it).

        A queue carried over from a previous, already-closed loop is thrown
        away and rebuilt: reusing it makes every get()/put() raise
        "Queue ... is bound to a different event loop", which is what the
        second and every later download in the GUI used to hit.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop to attach a consumer to; enqueue_progress() drops the
            # event rather than crashing the caller that reported it.
            return

        if cls._event_queue is None or cls._queue_loop is not loop:
            cls._event_queue = asyncio.Queue()
            cls._queue_loop = loop
            # The old consumer belonged to the old loop; it can neither be
            # awaited nor cancelled from here, and it is already dead with
            # its loop, so just let go of it.
            cls._worker_task = None

        if cls._worker_task is not None and not cls._worker_task.done():
            return

        cls._worker_task = asyncio.create_task(cls._process_events_async())

    @classmethod
    async def stop_worker(cls) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if cls._queue_loop is not None and cls._queue_loop is not loop:
            # Left over from a loop that is gone: nothing here can be awaited
            # on it, so drop the references and let the next run rebuild them.
            cls._event_queue = None
            cls._queue_loop = None
            cls._worker_task = None
            return

        if cls._worker_task is not None:
            if cls._event_queue is not None:
                # sblocca il .get() in attesa nel task consumer
                await cls._event_queue.put(None)
            try:
                await asyncio.wait_for(cls._worker_task, timeout=5)
            except asyncio.TimeoutError:
                cls._worker_task.cancel()
            except RuntimeError:
                # Consumer already died with a stale loop; nothing to drain.
                pass
            cls._worker_task = None

    @classmethod
    async def _process_events_async(cls) -> None:
        assert cls._event_queue is not None
        while True:
            event = await cls._event_queue.get()
            if event is None:
                break

            item_id, track_name, current_bytes, total_bytes = event

            if not progress_bars_enabled():
                cls._log_progress(item_id, track_name, current_bytes, total_bytes)
                continue

            try:
                with tqdm.get_lock():
                    bar = cls._bars.get(item_id)
                    if bar is None:
                        bar = cls.create_bar(item_id, track_name, total_bytes)
                    if bar is None:
                        continue

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
                logging.getLogger(__name__).exception(
                    "ProgressManager consumer crashed",
                )

    @classmethod
    def _log_progress(
        cls,
        item_id: str,
        track_name: str,
        current_bytes: int,
        total_bytes: int | None,
    ) -> None:
        """Textual stand-in for the bar, for logs that can't render one.

        Throttled twice over — by percentage and by time — so a track costs a
        handful of lines instead of one per received chunk. The 100% line is
        left out on purpose: the result line printed once the download
        finishes already reports it, with the provider and the final size.
        """
        if not total_bytes or current_bytes >= total_bytes:
            cls._progress_log_state.pop(item_id, None)
            return

        percent = int(current_bytes * 100 / total_bytes)
        now = time.time()
        # Starting at 0% means the first line is only drawn one step in: that a
        # track started is already on the track header line above it.
        last_time, last_percent = cls._progress_log_state.get(item_id, (0.0, 0))
        if (
            percent - last_percent < _PROGRESS_LOG_PERCENT_STEP
            or now - last_time < _PROGRESS_LOG_MIN_INTERVAL_S
        ):
            return

        cls._progress_log_state[item_id] = (now, percent)
        print_track_progress(track_name, percent, current_bytes, total_bytes)

    @classmethod
    def enqueue_progress(
        cls,
        item_id: str,
        track_name: str,
        current_bytes: int,
        total_bytes: int | None,
    ) -> None:
        """Called from a coroutine/callback inside the main loop. The worker
        is started lazily the first time (idempotent) and then a simple
        put_nowait() is used — no thread-safe primitive is required.
        """
        cls.start_worker()
        if cls._event_queue is not None:
            with contextlib.suppress(Exception):
                cls._event_queue.put_nowait(
                    (item_id, track_name, current_bytes, total_bytes),
                )

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
    def create_bar(
        cls,
        item_id: str,
        track_name: str,
        total_bytes: int | None,
    ) -> tqdm | None:
        """Returns the bar for *item_id*, or None when bars have no screen.

        A disabled tqdm draws nothing, but it still allocates a slot, holds
        a file handle and takes part in the position bookkeeping — all of it
        pointless once a sink owns the output, so with one installed no bar
        is built at all and the callers fall back to the textual path.
        """
        if item_id in cls._bars:
            return cls._bars[item_id]
        if sink_active():
            return None

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
            disable=not progress_bars_enabled(),
        )

        cls._bars[item_id] = bar
        return bar

    @classmethod
    def release_bar(cls, item_id: str) -> None:
        cls._progress_log_state.pop(item_id, None)
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
            cls._progress_log_state.clear()
            cls.clear_master_bar()

    @classmethod
    def initialize_master_bar(
        cls,
        total_items: int,
        description: str = "Progress",
        at_top: bool = True,
    ) -> None:
        if not at_top:
            msg = "Only top-aligned master bar is supported by ProgressManager at this time."
            raise ValueError(
                msg,
            )
        with tqdm.get_lock():
            cls.clear_master_bar()
            if sink_active():
                return
            cls._master_enabled = True
            cls._master_bar = tqdm(
                total=total_items,
                desc=description,
                leave=True,
                position=0,
                dynamic_ncols=True,
                miniters=1,
                file=sys.__stderr__,
                disable=not progress_bars_enabled(),
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
    """Progress callback passed to providers. Unchanged in throttling logic;
    enqueue_progress() is now thread-agnostic by design (asyncio.Queue), so it
    remains synchronous and non-blocking as before.
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
            self._item_id,
            self._track_name,
            current_bytes,
            total_bytes,
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
                self._item_id,
                current_mb,
                total_mb,
                speed_mbps,
            ),
        )

    @classmethod
    def clear_item(cls, item_id: str) -> None:
        ProgressManager.clear_item(item_id)


RichProgressCallback = ProgressCallback
