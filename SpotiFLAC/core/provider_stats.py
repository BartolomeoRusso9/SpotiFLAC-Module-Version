from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .atomic_io import write_json_atomic
from .paths import cache_path

#: Key prefix used by the download path (see downloader.download_one_async),
#: as opposed to the per-API-URL keys a provider uses internally for its own
#: endpoint rotation. Both live in the same file; the prefix is what tells
#: the health view which is which.
PROVIDER_KIND = "provider"

_CACHE_FILE_NAME = "provider_priority.json"


def _get_cache_file() -> Path:
    return cache_path(_CACHE_FILE_NAME)


def get_cache_path() -> Path:
    return _get_cache_file()


def _ensure_cache_dir() -> None:
    _get_cache_file().parent.mkdir(parents=True, exist_ok=True)


# Sync helper functions for the worker threads
def _load_cache_sync() -> dict[str, dict]:
    try:
        cache_file = _get_cache_file()
        if cache_file.exists():
            return json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_cache_sync(data: dict[str, dict]) -> None:
    try:
        _ensure_cache_dir()
        # Atomic: this is written from worker threads as downloads complete,
        # so a plain write_text() could be truncating the file at the moment
        # another thread reads it.
        write_json_atomic(_get_cache_file(), data)
    except OSError:
        pass


@dataclass
class _ProviderStats:
    successes: int = 0
    failures: int = 0
    last_success: float = 0.0
    last_failure: float = 0.0
    last_attempt: float = 0.0
    last_outcome: str = ""
    #: Exponential moving average of successful-attempt duration, in seconds.
    #: A moving average rather than a total/count pair because what anyone
    #: looking at this wants to know is "is it slow *now*" — an endpoint that
    #: was fast for a thousand downloads last month and is timing out today
    #: should not read as fast.
    avg_duration_s: float = 0.0
    last_duration_s: float = 0.0
    #: Truncated: this is written to a JSON file under $HOME and rendered in
    #: a UI, and a provider can put anything in an error string.
    last_error: str = ""

    #: Weight of the newest sample in avg_duration_s.
    _EMA_ALPHA = 0.3

    def observe(self, success: bool, duration_s: float, error: str = "") -> None:
        now = time.time()
        if success:
            self.successes += 1
            self.last_success = now
            self.last_outcome = "success"
            self.last_error = ""
            if duration_s > 0:
                self.last_duration_s = duration_s
                self.avg_duration_s = (
                    duration_s
                    if self.avg_duration_s <= 0
                    else (
                        self._EMA_ALPHA * duration_s
                        + (1 - self._EMA_ALPHA) * self.avg_duration_s
                    )
                )
        else:
            self.failures += 1
            self.last_failure = now
            self.last_outcome = "failure"
            # last_duration_s tracks the most recent attempt of any kind (a
            # timeout has a meaningful duration too); only avg_duration_s stays
            # success-only, so a run of failures can't skew the latency EMA.
            if duration_s > 0:
                self.last_duration_s = duration_s
            if error:
                self.last_error = str(error)[:300]
        self.last_attempt = now

    @property
    def attempts(self) -> int:
        return self.successes + self.failures

    def success_rate(self) -> float | None:
        return (self.successes / self.attempts) if self.attempts else None

    def score(self) -> float:
        base = self.successes - (self.failures * 2)
        now = time.time()
        if self.last_failure > 0 and (now - self.last_failure) < 300:
            base -= 10
        if self.last_success > 0 and (now - self.last_success) < 300:
            base += 5
        return float(base)

    @classmethod
    def from_dict(cls, data: dict) -> _ProviderStats:
        return cls(
            successes=int(data.get("successes", 0)),
            failures=int(data.get("failures", 0)),
            last_success=float(data.get("last_success", 0.0)),
            last_failure=float(data.get("last_failure", 0.0)),
            last_attempt=float(data.get("last_attempt", 0.0)),
            last_outcome=str(data.get("last_outcome", "")) or "",
            avg_duration_s=float(data.get("avg_duration_s", 0.0)),
            last_duration_s=float(data.get("last_duration_s", 0.0)),
            last_error=str(data.get("last_error", "")) or "",
        )

    def to_dict(self) -> dict:
        return asdict(self)


class ProviderScorer:
    """Async thread-safe manager that tracks successes/failures per API URL.
    Uses lazy initialization to support asyncio operations.
    """

    def __init__(self) -> None:
        self._stats: dict[str, _ProviderStats] = {}
        self._stats_lock = asyncio.Lock()
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def _ensure_initialized(self) -> None:
        """Loads the database only the first time it's requested."""
        if not self._initialized:
            async with self._init_lock:
                if not self._initialized:
                    cache = await asyncio.to_thread(_load_cache_sync)
                    for key, raw in cache.items():
                        try:
                            self._stats[key] = _ProviderStats.from_dict(raw)
                        except Exception:
                            continue
                    self._initialized = True

    async def _persist_to_disk_async(self) -> None:
        cache = {key: stat.to_dict() for key, stat in self._stats.items()}
        await asyncio.to_thread(_save_cache_sync, cache)

    async def _record_async(
        self,
        provider_type: str,
        api_url: str,
        success: bool,
        duration_s: float = 0.0,
        error: str = "",
    ) -> None:
        await self._ensure_initialized()
        key = f"{provider_type}:{api_url}"

        async with self._stats_lock:
            stats = self._stats.setdefault(key, _ProviderStats())
            stats.observe(success, duration_s, error)

            # Scrittura su disco delegata a un worker
            await self._persist_to_disk_async()

    async def record_success_async(
        self, provider_type: str, api_url: str, duration_s: float = 0.0
    ) -> None:
        await self._record_async(provider_type, api_url, True, duration_s)

    async def record_failure_async(
        self,
        provider_type: str,
        api_url: str,
        duration_s: float = 0.0,
        error: str = "",
    ) -> None:
        await self._record_async(provider_type, api_url, False, duration_s, error)

    async def prioritize_async(
        self,
        provider_type: str,
        api_urls: list[str],
    ) -> list[str]:
        await self._ensure_initialized()

        async with self._stats_lock:
            original_index = {url: idx for idx, url in enumerate(api_urls)}

            def _rank(url: str) -> tuple[int, float, float, int]:
                key = f"{provider_type}:{url}"
                s = self._stats.get(key)
                if s is None:
                    return (1, 0.0, 0.0, original_index.get(url, 0))

                outcome_rank = 1
                if s.last_outcome == "success":
                    outcome_rank = 2
                elif s.last_outcome == "failure":
                    outcome_rank = 0

                last_attempt = max(s.last_success, s.last_failure, s.last_attempt)
                return (
                    outcome_rank,
                    s.last_success,
                    last_attempt,
                    -original_index.get(url, 0),
                )

            return sorted(api_urls, key=_rank, reverse=True)

    async def reset_async(self) -> None:
        """Utile per i test o reset manuale."""
        await self._ensure_initialized()
        async with self._stats_lock:
            self._stats.clear()
            await asyncio.to_thread(_save_cache_sync, {})


# Istanza Singleton globale
_scorer = ProviderScorer()


async def record_success_async(
    provider_type: str, api_url: str, duration_s: float = 0.0
) -> None:
    await _scorer.record_success_async(provider_type, api_url, duration_s)


async def record_failure_async(
    provider_type: str, api_url: str, duration_s: float = 0.0, error: str = ""
) -> None:
    await _scorer.record_failure_async(provider_type, api_url, duration_s, error)


async def record_provider_attempt_async(
    provider_name: str,
    success: bool,
    duration_s: float = 0.0,
    error: str = "",
) -> None:
    """Records one download attempt against a named provider/extension.

    This is what the download path calls (see downloader.download_one_async).
    Until it existed, this module recorded nothing at all unless a
    third-party Python extension happened to call it directly — so
    /api/metrics reported an empty `providers` map on every ordinary install,
    and the ordering in prioritize_async() had nothing to order by.

    Never raises: bookkeeping must not be able to fail a download.
    """
    try:
        await _scorer._record_async(
            PROVIDER_KIND, provider_name, success, duration_s, error
        )
    except Exception:
        pass


async def prioritize_async(provider_type: str, api_urls: list[str]) -> list[str]:
    return await _scorer.prioritize_async(provider_type, api_urls)


# Alias for global async compatibility
prioritize_providers_async = prioritize_async


def snapshot() -> dict:
    """Everything the scorer has learned, as plain data.

    Read straight off disk rather than through the async scorer: this backs
    the /metrics endpoint, which must not have to reach into another
    component's event loop just to read a JSON file it already owns.
    """
    raw = _load_cache_sync()
    providers: dict[str, dict] = {}
    totals = {"successes": 0, "failures": 0}

    for key, entry in raw.items():
        # Keys are f"{provider_type}:{api_url}" (see _record_async), and the
        # URL contains colons of its own — so split on the first one only.
        provider_type, _, api_url = key.partition(":")
        stats = _ProviderStats.from_dict(entry)
        totals["successes"] += stats.successes
        totals["failures"] += stats.failures
        providers.setdefault(provider_type or key, {})[api_url or key] = {
            **stats.to_dict(),
            "score": stats.score(),
        }

    attempts = totals["successes"] + totals["failures"]
    return {
        "providers": providers,
        "totals": {
            **totals,
            "attempts": attempts,
            "success_rate": (totals["successes"] / attempts) if attempts else None,
        },
    }


def health() -> list[dict]:
    """One row per download provider/extension, worst first.

    A projection of the same file `snapshot()` reads, narrowed to the keys the
    download path writes (see PROVIDER_KIND) and shaped for a person rather
    than for a metrics scraper: which extension is failing, how often, how
    slow it is, and what it said last time it broke.

    Ordered by success rate ascending so whatever is actually broken is at the
    top — a health panel sorted alphabetically makes you hunt for the problem
    it exists to show you. Providers never attempted sit at the end.
    """
    rows: list[dict] = []
    for key, entry in _load_cache_sync().items():
        kind, _, name = key.partition(":")
        if kind != PROVIDER_KIND or not name:
            continue
        stats = _ProviderStats.from_dict(entry)
        rows.append(
            {
                "provider": name,
                "attempts": stats.attempts,
                "successes": stats.successes,
                "failures": stats.failures,
                "success_rate": stats.success_rate(),
                "avg_duration_s": round(stats.avg_duration_s, 2),
                "last_duration_s": round(stats.last_duration_s, 2),
                "last_outcome": stats.last_outcome,
                "last_success": stats.last_success,
                "last_failure": stats.last_failure,
                "last_attempt": stats.last_attempt,
                "last_error": stats.last_error,
                "score": stats.score(),
            }
        )

    rows.sort(
        key=lambda r: (
            # Never-attempted last, then worst success rate first, then the
            # busiest of equally-healthy providers.
            r["attempts"] == 0,
            r["success_rate"] if r["success_rate"] is not None else 1.0,
            -r["attempts"],
        )
    )
    return rows
