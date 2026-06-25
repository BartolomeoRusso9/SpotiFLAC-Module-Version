from __future__ import annotations

import json
import os
import asyncio
import time
from dataclasses import asdict, dataclass
from pathlib import Path

_CACHE_DIR_NAME = "spotiflac"
_CACHE_FILE_NAME = "provider_priority.json"


def _get_cache_file() -> Path:
    override = os.getenv("SPOTIFLAC_CACHE_DIR")
    if override:
        return Path(override) / _CACHE_FILE_NAME

    xdg_cache_home = os.getenv("XDG_CACHE_HOME")
    if xdg_cache_home:
        return Path(xdg_cache_home) / _CACHE_DIR_NAME / _CACHE_FILE_NAME

    return Path.home() / ".cache" / _CACHE_DIR_NAME / _CACHE_FILE_NAME


def get_cache_path() -> Path:
    return _get_cache_file()


def _ensure_cache_dir() -> None:
    _get_cache_file().parent.mkdir(parents=True, exist_ok=True)


# Funzioni di supporto sincrone per i thread worker
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
        cache_file = _get_cache_file()
        _ensure_cache_dir()
        cache_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
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

    def score(self) -> float:
        base = self.successes - (self.failures * 2)
        now = time.time()
        if self.last_failure > 0 and (now - self.last_failure) < 300:
            base -= 10
        if self.last_success > 0 and (now - self.last_success) < 300:
            base += 5
        return float(base)

    @classmethod
    def from_dict(cls, data: dict) -> "_ProviderStats":
        return cls(
            successes=int(data.get("successes", 0)),
            failures=int(data.get("failures", 0)),
            last_success=float(data.get("last_success", 0.0)),
            last_failure=float(data.get("last_failure", 0.0)),
            last_attempt=float(data.get("last_attempt", 0.0)),
            last_outcome=str(data.get("last_outcome", "")) or "",
        )

    def to_dict(self) -> dict:
        return asdict(self)


class ProviderScorer:
    """
    Gestore thread-safe asincrono che traccia successi/fallimenti per API URL.
    Usa l'inizializzazione lazy per supportare operazioni asyncio.
    """

    def __init__(self) -> None:
        self._stats: dict[str, _ProviderStats] = {}
        self._stats_lock = asyncio.Lock()
        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def _ensure_initialized(self) -> None:
        """Carica il database solo la prima volta che viene richiesto."""
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
        self, provider_type: str, api_url: str, success: bool
    ) -> None:
        await self._ensure_initialized()
        key = f"{provider_type}:{api_url}"
        now = time.time()

        async with self._stats_lock:
            s = self._stats.setdefault(key, _ProviderStats())
            if success:
                s.successes += 1
                s.last_success = now
                s.last_outcome = "success"
            else:
                s.failures += 1
                s.last_failure = now
                s.last_outcome = "failure"
            s.last_attempt = now

            # Scrittura su disco delegata a un worker
            await self._persist_to_disk_async()

    async def record_success_async(self, provider_type: str, api_url: str) -> None:
        await self._record_async(provider_type, api_url, True)

    async def record_failure_async(self, provider_type: str, api_url: str) -> None:
        await self._record_async(provider_type, api_url, False)

    async def prioritize_async(
        self, provider_type: str, api_urls: list[str]
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


async def record_success_async(provider_type: str, api_url: str) -> None:
    await _scorer.record_success_async(provider_type, api_url)


async def record_failure_async(provider_type: str, api_url: str) -> None:
    await _scorer.record_failure_async(provider_type, api_url)


async def prioritize_async(provider_type: str, api_urls: list[str]) -> list[str]:
    return await _scorer.prioritize_async(provider_type, api_urls)


# Alias for global async compatibility
prioritize_providers_async = prioritize_async
