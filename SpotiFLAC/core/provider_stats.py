"""
Sistema di scoring per le API dei provider.
Porta il pattern Go prioritizeProviders/recordProviderSuccess/Failure.

Le API che falliscono vengono messe in fondo alla lista automaticamente,
quelle che funzionano vengono promosse in cima — senza shuffle casuale.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class _ProviderStats:
    successes:    int   = 0
    failures:     int   = 0
    last_success: float = 0.0
    last_failure: float = 0.0

    def score(self) -> float:
        base = self.successes - (self.failures * 2)
        now  = time.time()
        if self.last_failure > 0 and (now - self.last_failure) < 300:
            base -= 10
        if self.last_success > 0 and (now - self.last_success) < 300:
            base += 5
        return float(base)


class ProviderScorer:
    """
    Singleton thread-safe che traccia successi/fallimenti per API URL.
    Equivalente a recordProviderSuccess/recordProviderFailure del Go.
    """
    _instance: "ProviderScorer | None" = None
    _lock = threading.Lock()

    def __new__(cls) -> "ProviderScorer":
        with cls._lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._stats: dict[str, _ProviderStats] = {}
                inst._stats_lock = threading.Lock()
                cls._instance = inst
        return cls._instance

    def record_success(self, provider_type: str, api_url: str) -> None:
        key = f"{provider_type}:{api_url}"
        with self._stats_lock:
            s = self._stats.setdefault(key, _ProviderStats())
            s.successes    += 1
            s.last_success  = time.time()

    def record_failure(self, provider_type: str, api_url: str) -> None:
        key = f"{provider_type}:{api_url}"
        with self._stats_lock:
            s = self._stats.setdefault(key, _ProviderStats())
            s.failures    += 1
            s.last_failure = time.time()

    def prioritize(self, provider_type: str, api_urls: list[str]) -> list[str]:
        with self._stats_lock:
            def _score(url: str) -> float:
                key = f"{provider_type}:{url}"
                s   = self._stats.get(key)
                if s is None:
                    return 0.0   # API nuova: neutro, non penalizzata
                sc = s.score()
                # Se negativo, non scendere sotto -1 per non sparire in fondo
                return max(sc, -1.0)   # ← FIX: floor a -1

            return sorted(api_urls, key=_score, reverse=True)

    def reset(self) -> None:
        """Utile per i test."""
        with self._stats_lock:
            self._stats.clear()


# Singleton globale — usato dai provider
_scorer = ProviderScorer()


def record_success(provider_type: str, api_url: str) -> None:
    _scorer.record_success(provider_type, api_url)


def record_failure(provider_type: str, api_url: str) -> None:
    _scorer.record_failure(provider_type, api_url)


def prioritize(provider_type: str, api_urls: list[str]) -> list[str]:
    return _scorer.prioritize(provider_type, api_urls)

# Alias per compatibilità con i provider
prioritize_providers = prioritize
