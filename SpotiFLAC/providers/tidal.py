"""
TidalProvider — implementazione migliorata, robusta e tipizzata.
"""

from __future__ import annotations

import asyncio
import base64
import difflib
import json
import logging
import os
import random
import re
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import quote

import httpx

try:
    import aiofiles
except ImportError:
    aiofiles = None

from ..core.console import (
    print_api_failure,
    print_quality_fallback,
    print_source_banner,
)
from ..core.download_validation import validate_downloaded_track_async
from ..core.endpoints import (
    get_community_url,
    get_monochrome_token,
    get_tidal_post_endpoints,
)
from ..core.errors import ErrorKind, ParseError, SpotiflacError, TrackNotFoundError
from ..core.flac_validation import validate_and_repair_if_needed
from ..core.http import NetworkManager, RetryConfig, async_zarz_rate_limiter
from ..core.link_resolver import LinkResolver
from ..core.models import DownloadResult, TrackMetadata
from ..core.musicbrainz import fetch_mb_metadata_async, mb_result_to_tags
from ..core.quality import normalize_quality as _cq_normalize_quality
from ..core.quality import quality_fallback_chain as _cq_quality_fallback_chain
from ..core.tagger import EmbedOptions, _print_mb_summary, embed_metadata_async
from .base import BaseProvider
from .qobuz import _API_BASE as QOBUZ_API_BASE
from .qobuz import _compute_signature, _scrape_credentials_async

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TIDAL_APIS_GET = []

_TIDAL_API_POST = get_tidal_post_endpoints()

_TIDAL_COMMUNITY_URL = ""
try:
    _TIDAL_COMMUNITY_URL = get_community_url("tidal")
except Exception:
    pass
if _TIDAL_COMMUNITY_URL:
    _TIDAL_API_POST = list(_TIDAL_API_POST) + [_TIDAL_COMMUNITY_URL]

_CLEAN_POST_APIS = frozenset(a.rstrip("/") for a in _TIDAL_API_POST)

_TIDAL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

_POST_USER_AGENT = ["SpotiFLAC-Mobile/4.5.0"]

# Headers specifici per endpoint "community" (usati nelle POST verso mirror comunitari)
_COMMUNITY_POST_HEADERS = {
    "User-Agent": "SpotiFLAC/7.1.9",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "x-api-key": "explore-obscure-chivalry-travesty-blinks",
}

_TIDAL_PROXY_BASE = "https://tidal-proxy.monochrome.tf/api/v1"
_TIDAL_API_GIST_URL = (
    "https://gist.githubusercontent.com/afkarxyz/2ce772b943321b9448b454f39403ce25/raw"
)
_TIDAL_API_CACHE_FILE = "tidal-api-urls.json"

_API_TIMEOUT_S = 8
_MAX_RETRIES = 2
_RETRY_DELAY_S = 0.3
_RETRY_JITTER_S = 0.4
_RATE_LIMIT_DEFAULT = 5.0

# ---------------------------------------------------------------------------
# Per-API rate-limit registry
# ---------------------------------------------------------------------------

_api_cooldown_lock: threading.Lock = threading.Lock()
_api_cooldown_until: dict[str, float] = {}


def _is_deterministic_error(message: str) -> bool:
    """Check if the returned error is caused by the track and not by a network timeout"""
    text = str(message or "")
    if not text:
        return False
    return bool(
        re.search(
            r"EAC3_JOC|did not report|PREVIEW asset|Invalid TIDAL|assetPresentation|missing manifest|returned no data",
            text,
            re.IGNORECASE,
        )
    )


def _clean_title(value: str) -> str:
    """Pulisce il titolo in maniera approfondita, rimuovendo parentesi ed accenti (come index.js)"""
    cleaned = str(value or "")
    patterns = [
        "remaster",
        "remastered",
        "deluxe",
        "bonus",
        "single",
        "album version",
        "radio edit",
        "original mix",
        "extended",
        "club mix",
        "remix",
        "live",
        "acoustic",
        "demo",
    ]

    changed = True
    while changed:
        changed = False

        def replacer(match: re.Match[str]) -> str:
            nonlocal changed
            content = match.group(0).lower()
            for p in patterns:
                if p in content:
                    changed = True
                    return " "
            return match.group(0)

        cleaned = re.sub(r"\([^)]*\)|\[[^\]]*\]", replacer, cleaned)

    # Rimuovi i diacritici e formatta come in JS (normalizeLooseTitle)
    try:
        cleaned = unicodedata.normalize("NFD", cleaned)
        cleaned = "".join(c for c in cleaned if unicodedata.category(c) != "Mn")
    except Exception:
        pass
    cleaned = re.sub(r"[\/\\_\-|.&+]", " ", cleaned)
    cleaned = re.sub(r"[^\w\s]+", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip().lower()


def _mark_api_rate_limited(api_url: str, wait_s: float) -> None:
    key = api_url.rstrip("/")
    with _api_cooldown_lock:
        _api_cooldown_until[key] = time.time() + wait_s
    logger.debug("[tidal] API rate-limited per %.1fs", wait_s)


def _is_api_rate_limited(api_url: str) -> bool:
    key = api_url.rstrip("/")
    with _api_cooldown_lock:
        until = _api_cooldown_until.get(key, 0.0)
    return time.time() < until


def _clear_api_rate_limit(api_url: str) -> None:
    key = api_url.rstrip("/")
    with _api_cooldown_lock:
        _api_cooldown_until.pop(key, None)


# ---------------------------------------------------------------------------
# ISRC helper
# ---------------------------------------------------------------------------


async def _find_isrc_via_qobuz(
    title: str, artist: str, duration_ms: int = 0
) -> str | None:
    """Search for the ISRC on Qobuz with fuzzy verification of title, artist, and duration."""
    try:
        creds = await _scrape_credentials_async()
        query = f"{title} {artist}".strip()
        params = {"query": query, "limit": "5"}
        timestamp = str(int(time.time()))
        signature = _compute_signature(
            "track/search", params, timestamp, creds.app_secret
        )
        req_params = {
            **params,
            "app_id": creds.app_id,
            "request_ts": timestamp,
            "request_sig": signature,
        }
        url = f"{QOBUZ_API_BASE}/track/search"
        headers = {"X-App-Id": creds.app_id}

        client = await NetworkManager.get_async_client_safe()
        resp = await client.get(url, params=req_params, headers=headers, timeout=8)
        if resp.status_code != 200:
            return None

        items = resp.json().get("tracks", {}).get("items", [])
        if not items:
            return None

        clean_title = _clean_title(title)
        clean_artist = artist.split(",")[0].strip().lower()

        best_item: dict | None = None
        best_score: float = 0.0

        for item in items:
            t_title = _clean_title(item.get("title", ""))
            performer = item.get("performer") or {}
            t_artist = (
                performer.get("name", "").lower() if isinstance(performer, dict) else ""
            )

            score = difflib.SequenceMatcher(None, clean_title, t_title).ratio() * 60
            score += difflib.SequenceMatcher(None, clean_artist, t_artist).ratio() * 40

            if duration_ms and item.get("duration", 0) > 0:
                dur_diff = abs(item["duration"] * 1000 - duration_ms)
                if dur_diff <= 3_000:
                    score += 20
                elif dur_diff > 10_000:
                    score -= 30

            if score > best_score:
                best_score = score
                best_item = item

        # soglia minima per evitare falsi positivi
        if best_item and best_score >= 50:
            return best_item.get("isrc")

        logger.debug(
            "[tidal] Qobuz ISRC: no confident match (best score %.1f)", best_score
        )
        return None

    except Exception as exc:
        logger.debug("[tidal] Qobuz ISRC lookup failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Quality helpers
# ---------------------------------------------------------------------------


def _normalize_quality(value: str) -> str:
    return _cq_normalize_quality(value)


_QUALITY_FALLBACK_CHAINS: dict[str, list[str]] = {
    "DOLBY_ATMOS": ["DOLBY_ATMOS", "HI_RES_LOSSLESS", "LOSSLESS", "HIGH", "LOW"],
    "HI_RES_LOSSLESS": ["HI_RES_LOSSLESS", "LOSSLESS", "HIGH", "LOW"],
    "LOSSLESS": ["LOSSLESS", "HIGH", "LOW"],
    "HIGH": ["HIGH", "LOW"],
    "LOW": ["LOW"],
}


def _quality_fallback_chain(quality: str) -> list[str]:
    return _cq_quality_fallback_chain(quality)


# ---------------------------------------------------------------------------
# API list manager
# ---------------------------------------------------------------------------

_tidal_api_list_mu: threading.Lock = threading.Lock()
_tidal_api_list_state: dict[str, Any] | None = None


def _get_cache_path() -> Path:
    cache_dir = Path.home() / ".cache" / "spotiflac"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / _TIDAL_API_CACHE_FILE


def _clone_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "urls": list(state.get("urls", [])),
        "last_used_url": state.get("last_used_url", ""),
        "updated_at": state.get("updated_at", 0),
        "source": state.get("source", ""),
    }


def _normalize_tidal_api_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for raw in urls:
        url = raw.strip().rstrip("/")
        if not url or url in seen:
            continue
        seen.add(url)
        normalized.append(url)
    return normalized


def _load_tidal_api_list_state_locked() -> dict[str, Any]:
    global _tidal_api_list_state
    if _tidal_api_list_state is not None:
        return _clone_state(_tidal_api_list_state)

    cache_path = _get_cache_path()
    empty = {"urls": [], "last_used_url": "", "updated_at": 0, "source": ""}

    if not cache_path.exists():
        _tidal_api_list_state = _clone_state(empty)
        return _clone_state(empty)

    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            state = json.load(f)
        state["urls"] = _normalize_tidal_api_urls(state.get("urls", []))
        _tidal_api_list_state = _clone_state(state)
        return _clone_state(state)
    except Exception as exc:
        logger.warning("[tidal] failed to read API list cache: %s", exc)
        _tidal_api_list_state = _clone_state(empty)
        return _clone_state(empty)


def _save_tidal_api_list_state_locked(state: dict[str, Any]) -> None:
    global _tidal_api_list_state
    cache_path = _get_cache_path()
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        _tidal_api_list_state = _clone_state(state)
    except Exception as exc:
        logger.warning("[tidal] failed to write API list cache: %s", exc)


def _fetch_tidal_api_urls_from_gist() -> list[str]:
    resp = httpx.get(
        _TIDAL_API_GIST_URL, timeout=10, headers={"User-Agent": _TIDAL_USER_AGENT}
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Tidal API gist returned status {resp.status_code}")

    try:
        payload = resp.json()
    except Exception:
        raise RuntimeError(f"Tidal API gist returned non-JSON: {resp.text[:120]}")

    if not isinstance(payload, list):
        if isinstance(payload, dict):
            urls = payload.get("apis") or payload.get("urls") or list(payload.values())
            if urls and isinstance(urls, list):
                payload = urls
            else:
                raise RuntimeError(
                    f"Tidal API gist returned unexpected format: {type(payload)}"
                )
        else:
            raise RuntimeError("Tidal API gist did not return a JSON array")

    urls = _normalize_tidal_api_urls(payload)
    if not urls:
        raise RuntimeError("Tidal API gist returned no valid URLs")
    return urls


async def _fetch_tidal_api_urls_from_gist_async() -> list[str]:
    client = await NetworkManager.get_async_client_safe()
    resp = await client.get(
        _TIDAL_API_GIST_URL, timeout=10, headers={"User-Agent": _TIDAL_USER_AGENT}
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Tidal API gist returned status {resp.status_code}")

    try:
        payload = resp.json()
    except Exception:
        raise RuntimeError(f"Tidal API gist returned non-JSON: {resp.text[:120]}")

    if not isinstance(payload, list):
        if isinstance(payload, dict):
            urls = payload.get("apis") or payload.get("urls") or list(payload.values())
            if urls and isinstance(urls, list):
                payload = urls
            else:
                raise RuntimeError(
                    f"Tidal API gist returned unexpected format: {type(payload)}"
                )
        else:
            raise RuntimeError("Tidal API gist did not return a JSON array")

    urls = _normalize_tidal_api_urls(payload)
    if not urls:
        raise RuntimeError("Tidal API gist returned no valid URLs")
    return urls


def _rotate_tidal_api_urls(urls: list[str], last_used_url: str) -> list[str]:
    normalized = _normalize_tidal_api_urls(urls)
    last_used_url = last_used_url.strip().rstrip("/")
    if len(normalized) < 2 or not last_used_url:
        return normalized
    try:
        last_index = normalized.index(last_used_url)
    except ValueError:
        return normalized
    return normalized[last_index + 1 :] + normalized[: last_index + 1]


def prime_tidal_api_list() -> None:
    try:
        refresh_tidal_api_list(force=True)
    except Exception as exc:
        logger.warning("[tidal] failed to refresh API list: %s", exc)
        with _tidal_api_list_mu:
            state = _load_tidal_api_list_state_locked()
            if not state["urls"]:
                state["urls"] = _normalize_tidal_api_urls(_TIDAL_APIS_GET)
                state["updated_at"] = int(time.time())
                state["source"] = "builtin-fallback"
                _save_tidal_api_list_state_locked(state)


def refresh_tidal_api_list(force: bool = False) -> list[str]:
    with _tidal_api_list_mu:
        state = _load_tidal_api_list_state_locked()
        if not force and state["urls"]:
            return list(state["urls"])
        try:
            gist_urls = _fetch_tidal_api_urls_from_gist()
        except Exception as exc:
            logger.warning("[tidal] gist fetch failed: %s", exc)
            gist_urls = []

        get_urls = _normalize_tidal_api_urls(_TIDAL_APIS_GET + gist_urls)
        post_urls = _normalize_tidal_api_urls(_TIDAL_API_POST)
        merged = get_urls + [u for u in post_urls if u not in set(get_urls)]

        if not merged:
            if state["urls"]:
                return list(state["urls"])
            raise RuntimeError("No Tidal API URLs available from any source")

        state["urls"] = merged
        state["updated_at"] = int(time.time())
        state["source"] = "builtin+gist"
        if state["last_used_url"] not in state["urls"]:
            state["last_used_url"] = ""
        _save_tidal_api_list_state_locked(state)
        return list(state["urls"])


def get_tidal_api_list() -> list[str]:
    with _tidal_api_list_mu:
        state = _load_tidal_api_list_state_locked()
        if not state["urls"]:
            raise RuntimeError("No cached Tidal API URLs")
        return list(state["urls"])


async def refresh_tidal_api_list_async(force: bool = False) -> list[str]:
    with _tidal_api_list_mu:
        state = _load_tidal_api_list_state_locked()
        if not force and state["urls"]:
            return list(state["urls"])

    try:
        gist_urls = await _fetch_tidal_api_urls_from_gist_async()
    except Exception as exc:
        logger.warning("[tidal] async gist fetch failed: %s", exc)
        gist_urls = []

    get_urls = _normalize_tidal_api_urls(_TIDAL_APIS_GET + gist_urls)
    post_urls = _normalize_tidal_api_urls(_TIDAL_API_POST)
    merged = get_urls + [u for u in post_urls if u not in set(get_urls)]

    with _tidal_api_list_mu:
        state = _load_tidal_api_list_state_locked()
        if not merged:
            if state["urls"]:
                return list(state["urls"])
            raise RuntimeError("No Tidal API URLs available from any source")

        state["urls"] = merged
        state["updated_at"] = int(time.time())
        state["source"] = "builtin+gist"
        if state["last_used_url"] not in state["urls"]:
            state["last_used_url"] = ""
        _save_tidal_api_list_state_locked(state)
        return list(state["urls"])


def get_rotated_tidal_api_list() -> list[str]:
    with _tidal_api_list_mu:
        state = _load_tidal_api_list_state_locked()
        if not state["urls"]:
            raise RuntimeError("No cached Tidal API URLs")
        return _rotate_tidal_api_urls(state["urls"], state["last_used_url"])


def remember_tidal_api_usage(api_url: str) -> None:
    with _tidal_api_list_mu:
        state = _load_tidal_api_list_state_locked()
        state["last_used_url"] = api_url.strip().rstrip("/")
        if state["updated_at"] == 0:
            state["updated_at"] = int(time.time())
        _save_tidal_api_list_state_locked(state)


# ---------------------------------------------------------------------------
# Manifest parsing
# ---------------------------------------------------------------------------


class ManifestResult(NamedTuple):
    direct_url: str
    init_url: str
    media_urls: list[str]
    mime_type: str
    sample_rate: int


def parse_manifest(manifest_b64: str) -> ManifestResult:
    try:
        raw = base64.b64decode(manifest_b64)
    except Exception as exc:
        raise ParseError("tidal", f"failed to decode manifest: {exc}", exc)

    text = raw.decode(errors="ignore").strip()

    if text.startswith("{"):
        try:
            data = json.loads(text)
            urls = data.get("urls", [])
            mime = data.get("mimeType", "")
            if urls:
                return ManifestResult(urls[0], "", [], mime, 0)
            raise ValueError("no URLs in BTS manifest")
        except Exception as exc:
            raise ParseError("tidal", f"BTS manifest parse failed: {exc}", exc)

    return _parse_dash_manifest(text)


def _parse_dash_manifest(text: str) -> ManifestResult:
    init_url = media_template = ""
    segment_count = 0
    sample_rate = 0

    sr_match = re.search(r'audioSamplingRate="(\d+)"', text, re.IGNORECASE)
    if sr_match:
        sample_rate = int(sr_match.group(1))

    try:
        mpd = ET.fromstring(text)
        ns = {"mpd": mpd.tag.split("}")[0].strip("{")} if "}" in mpd.tag else {}
        seg = mpd.find(".//mpd:SegmentTemplate", ns) or mpd.find(".//SegmentTemplate")
        if seg is not None:
            init_url = seg.get("initialization", "")
            media_template = seg.get("media", "")
            tl = seg.find("mpd:SegmentTimeline", ns) or seg.find("SegmentTimeline")
            if tl is not None:
                for s in tl.findall("mpd:S", ns) or tl.findall("S"):
                    segment_count += int(s.get("r") or 0) + 1
    except Exception:
        pass

    if not init_url or not media_template or segment_count == 0:
        m_init = re.search(r'initialization="([^"]+)"', text)
        m_media = re.search(r'media="([^"]+)"', text)
        if m_init:
            init_url = m_init.group(1)
        if m_media:
            media_template = m_media.group(1)
        for match in re.findall(r"<S\s+[^>]*>", text):
            r = re.search(r'r="(\d+)"', match)
            segment_count += int(r.group(1)) + 1 if r else 1

    if not init_url:
        raise ParseError("tidal", "no initialization URL found in DASH manifest")
    if segment_count == 0:
        raise ParseError("tidal", "no segments found in DASH manifest")

    init_url = init_url.replace("&amp;", "&")
    media_template = media_template.replace("&amp;", "&")
    media_urls = [
        media_template.replace("$Number$", str(i)) for i in range(1, segment_count + 1)
    ]

    return ManifestResult("", init_url, media_urls, "", sample_rate)


# ---------------------------------------------------------------------------
# Fetch singola API Tidal con retry + backoff esponenziale
# ---------------------------------------------------------------------------


async def _fetch_tidal_url_once_async(
    api: str,
    track_id: int,
    quality: str,
    timeout_s: int = _API_TIMEOUT_S,
) -> str:
    api_cleaning = api.rstrip("/")
    is_post_api = api_cleaning in _CLEAN_POST_APIS
    quality = _normalize_quality(quality)
    headers = {"User-Agent": _POST_USER_AGENT[0] if is_post_api else _TIDAL_USER_AGENT}
    # Se questa API è la community mirror, usa gli header dedicati per le POST
    try:
        if is_post_api and _TIDAL_COMMUNITY_URL and api_cleaning == _TIDAL_COMMUNITY_URL.rstrip("/"):
            headers = _COMMUNITY_POST_HEADERS
    except Exception:
        pass
    delay = _RETRY_DELAY_S
    last_err: Exception = RuntimeError("no attempts made")

    client = await NetworkManager.get_async_client_safe()

    if "anandserver.cfd" in api_cleaning:
        url = f"{api_cleaning}/api/stream/{track_id}"
        if quality in ("DOLBY_ATMOS", "HI_RES_LOSSLESS", "HI_RES"):
            url += "?strict_24=1"
        return url

    for attempt in range(_MAX_RETRIES + 1):
        if attempt > 0:
            jitter = random.uniform(0, _RETRY_JITTER_S)
            actual_delay = delay + jitter
            logger.debug(
                "[tidal] retry %d/%d after %.2fs", attempt, _MAX_RETRIES, actual_delay
            )
            await asyncio.sleep(actual_delay)
            delay *= 2

        try:
            if is_post_api and "zarz.moe" in api_cleaning:
                await async_zarz_rate_limiter.wait_for_slot()

            if is_post_api:
                if quality == "DOLBY_ATMOS":
                    resp = await client.post(
                        api_cleaning,
                        json={
                            "id": str(track_id),
                            "endpoint": "manifests",
                            "formats": ["EAC3_JOC"],
                        },
                        headers=headers,
                        timeout=timeout_s,
                    )
                    if resp.status_code == 429:
                        wait_s = float(
                            resp.headers.get("Retry-After", _RATE_LIMIT_DEFAULT)
                        )
                        _mark_api_rate_limited(api_cleaning, wait_s)
                        delay = max(delay, wait_s)
                        last_err = RuntimeError(
                            f"HTTP 429 (rate limited, retry-after={wait_s:.0f}s)"
                        )
                        continue
                    if resp.status_code != 200:
                        err_text = resp.text[:100]
                        try:
                            err_text = resp.json().get("message") or err_text
                        except Exception:
                            pass
                        last_err = RuntimeError(f"HTTP {resp.status_code}: {err_text}")
                        if _is_deterministic_error(str(last_err)):
                            break
                        continue

                    data = resp.json()
                    try:
                        attributes = data["data"]["data"]["attributes"]
                    except (KeyError, TypeError) as exc:
                        last_err = RuntimeError(
                            f"Atmos manifest payload missing attributes: {exc}"
                        )
                        break

                    formats = attributes.get("formats", [])
                    if "EAC3_JOC" not in [str(f).upper() for f in formats]:
                        last_err = RuntimeError(
                            "TIDAL API did not report EAC3_JOC for this track"
                        )
                        break

                    manifest_uri = attributes.get("uri", "").strip()
                    if not manifest_uri:
                        last_err = RuntimeError("Atmos manifest URI was empty")
                        break

                    mpd_resp = await client.get(
                        manifest_uri,
                        headers={
                            "Accept": "application/dash+xml,text/xml,application/xml;q=0.9,*/*;q=0.8",
                            "User-Agent": _TIDAL_USER_AGENT,
                        },
                        timeout=timeout_s,
                    )
                    mpd_resp.raise_for_status()
                    _clear_api_rate_limit(api_cleaning)
                    return "MANIFEST:" + base64.b64encode(mpd_resp.content).decode()

                resp = await client.post(
                    api_cleaning,
                    json={"id": str(track_id), "quality": quality},
                    headers=headers,
                    timeout=timeout_s,
                )
            else:
                url = f"{api_cleaning}/track/?id={track_id}&quality={quality}"
                resp = await client.get(url, headers=headers, timeout=timeout_s)

            if resp.status_code == 429:
                wait_s = float(resp.headers.get("Retry-After", _RATE_LIMIT_DEFAULT))
                _mark_api_rate_limited(api_cleaning, wait_s)
                delay = max(delay, wait_s)
                last_err = RuntimeError(
                    f"HTTP 429 (rate limited, retry-after={wait_s:.0f}s)"
                )
                continue

            if resp.status_code != 200:
                err_text = resp.text[:100]
                try:
                    p = resp.json()
                    err_text = p.get("message") or p.get("error") or err_text
                except Exception:
                    pass
                last_err = RuntimeError(f"HTTP {resp.status_code} - {err_text}")
                if _is_deterministic_error(str(last_err)):
                    break
                continue

            data = resp.json()

            if isinstance(data, dict):
                if data.get("success") is False:
                    last_err = RuntimeError(data.get("message") or "API Error")
                    if _is_deterministic_error(str(last_err)):
                        break
                    continue

                inner_data = data.get("data", {})
                manifest = (
                    inner_data.get("manifest") if isinstance(inner_data, dict) else None
                )

                if not manifest:
                    manifest = data.get("manifest")

                if manifest:
                    asset = (
                        inner_data.get("assetPresentation", "")
                        if isinstance(inner_data, dict)
                        else ""
                    )
                    if asset == "PREVIEW":
                        last_err = RuntimeError("returned PREVIEW instead of FULL")
                        break
                    _clear_api_rate_limit(api_cleaning)
                    return "MANIFEST:" + manifest

            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("OriginalTrackUrl"):
                        _clear_api_rate_limit(api_cleaning)
                        return str(item["OriginalTrackUrl"])

            last_err = RuntimeError("no download URL or manifest in response")
            if _is_deterministic_error(str(last_err)):
                break

        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            last_err = exc
            continue
        except Exception as exc:
            last_err = exc
            if _is_deterministic_error(str(last_err)):
                break
            continue

    raise last_err


async def _fetch_tidal_url_parallel_async(
    apis: list[str],
    track_id: int,
    quality: str,
    timeout_s: int = _API_TIMEOUT_S,
) -> tuple[str, str]:
    if not apis:
        raise SpotiflacError(ErrorKind.UNAVAILABLE, "no Tidal APIs configured", "tidal")

    available = [a for a in apis if not _is_api_rate_limited(a)]
    if not available:
        logger.debug("[tidal] tutte le API sono in cooldown, uso la lista completa")
        available = apis

    start = time.time()
    errors: list[str] = []
    tasks = {
        asyncio.create_task(
            _fetch_tidal_url_once_async(api, track_id, quality, timeout_s)
        ): api
        for api in available
    }

    pending: set = set()

    try:
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED, timeout=timeout_s + 2
        )

        if not done:
            raise SpotiflacError(
                ErrorKind.UNAVAILABLE,
                f"All {len(available)} Tidal APIs timed out "
                f"(of {len(apis)} total, {len(apis) - len(available)} in cooldown).",
                "tidal",
            )

        for task in done:
            api = tasks[task]
            try:
                dl_url = task.result()
                logger.debug(
                    "[tidal] parallel async: got URL in %.2fs", time.time() - start
                )
                for pending_task in pending:
                    pending_task.cancel()
                return api, dl_url
            except Exception as exc:
                err_msg = str(exc)[:80]
                errors.append(f"{api.rstrip('/')}: {err_msg}")
                print_api_failure("tidal", "", err_msg)

        raise SpotiflacError(
            ErrorKind.UNAVAILABLE,
            f"All Tidal APIs failed "
            f"(of {len(apis)} total, {len(apis) - len(available)} in cooldown).",
            "tidal",
        )

    finally:
        for pending_task in pending:
            pending_task.cancel()


# ---------------------------------------------------------------------------
# TidalProvider
# ---------------------------------------------------------------------------


class TidalProvider(BaseProvider):
    name = "tidal"
    _is_async = True

    def __init__(
        self,
        apis: list[str] | None = None,
        timeout_s: int = 15,
        qobuz_token: str | None = None,
        custom_api_url: str | None = None,
    ) -> None:
        """Create a Tidal provider without performing network I/O."""
        super().__init__(timeout_s=timeout_s, retry=RetryConfig(max_attempts=2))
        self._async_http._headers.update({"User-Agent": self._random_ua()})

        try:
            base_apis = apis or get_tidal_api_list()
        except Exception as exc:
            logger.warning(
                "[tidal] API list unavailable, using built-in fallback: %s", exc
            )
            base_apis = list(apis or _TIDAL_APIS_GET)

        if custom_api_url:
            clean = custom_api_url.strip().rstrip("/")
            base_apis = [clean] + [a for a in base_apis if a.rstrip("/") != clean]
            logger.info("[tidal] Custom API instance registered")

        self._apis = base_apis
        self._qobuz_token: str | None = qobuz_token or os.environ.get(
            "QOBUZ_AUTH_TOKEN"
        )

    @classmethod
    async def create_async(
        cls,
        apis: list[str] | None = None,
        timeout_s: int = 15,
        qobuz_token: str | None = None,
        custom_api_url: str | None = None,
    ) -> "TidalProvider":
        """Create a Tidal provider and refresh the remote API list asynchronously."""
        base_apis = apis
        if base_apis is None:
            try:
                base_apis = await refresh_tidal_api_list_async(force=True)
            except Exception as exc:
                logger.warning(
                    "[tidal] async API refresh failed, using constructor fallback: %s",
                    exc,
                )
                base_apis = None

        return cls(
            apis=base_apis,
            timeout_s=timeout_s,
            qobuz_token=qobuz_token,
            custom_api_url=custom_api_url,
        )

    async def resolve_spotify_to_tidal_async(
        self,
        spotify_track_id: str,
        track_name: str = "",
        artist_name: str = "",
        isrc: str = "",
        duration_ms: int = 0,
    ) -> str:
        if track_name and artist_name and track_name != "Unknown":
            result = await self._search_on_mirrors_async(
                track_name, artist_name, isrc, duration_ms
            )
            if result:
                return result
        logger.info("[tidal] mirror search failed — trying Songlink")
        return await self._resolve_via_songlink_async(spotify_track_id)

    async def _fetch_track_details_from_proxy(
        self, track_id: int, country_code: str = "US"
    ) -> dict:
        client = await NetworkManager.get_async_client_safe()
        url = f"{_TIDAL_PROXY_BASE}/tracks/{track_id}?countryCode={country_code}"
        headers = {
            "User-Agent": _TIDAL_USER_AGENT,
            "Accept": "application/json",
            "Authorization": get_monochrome_token(),
        }
        try:
            resp = await client.get(url, headers=headers, timeout=8)
            if resp.status_code == 200:
                return resp.json()
            else:
                print("[tidal] proxy returned %s", resp.status_code)
        except Exception as exc:
            print("[tidal] proxy request failed: %s", exc)
        return {}

    async def _search_on_mirrors_async(
        self,
        track_name: str,
        artist_name: str,
        isrc: str = "",
        duration_ms: int = 0,
    ) -> str | None:
        """
        Native async variant of _search_on_mirrors: uses the shared async HTTP client instead of self._session (sync), avoiding occupying an asyncio.to_thread thread-pool worker for the entire Tidal mirror search.
        """
        clean_track = _clean_title(track_name)
        clean_artist = artist_name.split(",")[0].strip()
        query = quote(f"{clean_artist} {clean_track}")

        client = await NetworkManager.get_async_client_safe()

        for api in self._apis:
            base = api.rstrip("/")
            for endpoint in [
                f"{base}/search/?s={query}&limit=5",
                f"{base}/search?s={query}&limit=5",
                f"{base}/search/track/?s={query}&limit=5",
            ]:
                try:
                    resp = await client.get(endpoint, timeout=7)
                    if resp.status_code == 429:
                        wait_s = float(
                            resp.headers.get("Retry-After", _RATE_LIMIT_DEFAULT)
                        )
                        _mark_api_rate_limited(base, wait_s)
                        logger.debug(
                            "[tidal] search rate-limited, skip (cooldown %.0fs)", wait_s
                        )
                        break
                    if resp.status_code != 200:
                        continue
                    t_id = self._extract_best_track_id(
                        resp.json(), track_name, clean_artist, isrc, duration_ms
                    )
                    if t_id:
                        _clear_api_rate_limit(base)
                        return f"https://listen.tidal.com/track/{t_id}"
                except Exception:
                    continue
        return None

    @staticmethod
    def _extract_best_track_id(
        data: Any,
        track_name: str,
        artist_name: str,
        isrc: str = "",
        duration_ms: int = 0,
    ) -> str | None:
        def _iter_items(d: Any) -> Any:
            if isinstance(d, list):
                yield from d
            elif isinstance(d, dict):
                for key in ("items", "tracks", "result", "results"):
                    inner = d.get(key)
                    if isinstance(inner, list):
                        yield from inner
                        return
                nested = d.get("data", {})
                if isinstance(nested, dict):
                    for key in ("items", "tracks", "results"):
                        inner = nested.get(key)
                        if isinstance(inner, list):
                            yield from inner
                            return
                if d.get("id") or d.get("trackId"):
                    yield d

        best_id = None
        best_score = 0.0
        clean_req_title = _clean_title(track_name)

        for item in _iter_items(data):
            if not isinstance(item, dict):
                continue
            t_id = str(item.get("id") or item.get("track_id") or "")
            if not t_id:
                continue

            if isrc and item.get("isrc", "").upper() == isrc.upper():
                return t_id

            t_title = item.get("title", "")
            t_title_clean = _clean_title(t_title)

            t_artist = ""
            artists_list = item.get("artists", [])
            if artists_list and isinstance(artists_list, list):
                t_artist = artists_list[0].get("name", "")
            elif item.get("artist") and isinstance(item.get("artist"), dict):
                t_artist = item.get("artist").get("name", "")

            t_dur = item.get("duration", 0) * 1000

            score = 0.0
            score += (
                difflib.SequenceMatcher(None, clean_req_title, t_title_clean).ratio()
                * 60
            )
            score += (
                difflib.SequenceMatcher(
                    None, artist_name.lower(), t_artist.lower()
                ).ratio()
                * 40
            )

            if duration_ms > 0 and t_dur > 0:
                if abs(duration_ms - t_dur) <= 10000:
                    score += 20

            if score > best_score:
                best_score = score
                best_id = t_id

        if best_id and best_score > 60:
            return best_id

        return None

    async def _resolve_via_songlink_async(self, spotify_track_id: str) -> str:
        """
        Native and purely asynchronous resolution via Songlink.
        Removes blocking and fixes the HTTP client AttributeError.
        """
        # Recupera il client asincrono corretto (lo stesso usato nel resto del file)
        client = await NetworkManager.get_async_client_safe()

        # Inizializza il resolver passandogli il client ASINCRONO
        resolver = LinkResolver(client)

        # Esegui direttamente l'await senza creare thread o sotto-loop artificiali
        links = await resolver.resolve_all_async(spotify_track_id)

        tidal_url = links.get("tidal")
        if tidal_url:
            return tidal_url

        raise TrackNotFoundError(self.name, spotify_track_id)

    async def _get_download_url_async(self, track_id: int, quality: str) -> str:
        from ..core.provider_stats import (
            prioritize_providers_async,
            record_success_async,
        )

        try:
            rotated = get_rotated_tidal_api_list()
        except Exception:
            rotated = self._apis

        ordered = await prioritize_providers_async("tidal", rotated)
        if self._apis and self._apis[0] not in ordered:
            ordered = [self._apis[0]] + ordered
        elif self._apis and ordered and self._apis[0] != ordered[0]:
            ordered = [self._apis[0]] + [a for a in ordered if a != self._apis[0]]

        winner_api, dl_url = await _fetch_tidal_url_parallel_async(
            ordered, track_id, quality, _API_TIMEOUT_S
        )
        await record_success_async("tidal", winner_api)
        remember_tidal_api_usage(winner_api)
        print_source_banner("tidal", "", quality)
        return dl_url

    async def _get_download_url_with_fallback_async(
        self, track_id: int, quality: str
    ) -> str:
        chain = _quality_fallback_chain(quality)
        last_exc: Exception = RuntimeError("no qualities attempted")

        for tier in chain:
            try:
                url = await self._get_download_url_async(track_id, tier)
                if tier != _normalize_quality(quality):
                    print_quality_fallback("tidal", _normalize_quality(quality), tier)
                    logger.warning(
                        "[tidal] quality downgraded from %s to %s", quality, tier
                    )
                return url
            except SpotiflacError as exc:
                last_exc = exc
                logger.warning(
                    "[tidal] %s unavailable, trying next tier: %s", tier, exc
                )
                continue

        raise last_exc

    async def _download_file_async(
        self, url_or_manifest: str, dest: Path, quality: str
    ) -> tuple[int, Path]:
        if url_or_manifest.startswith("MANIFEST:"):
            return await self._download_from_manifest_async(
                url_or_manifest.removeprefix("MANIFEST:"), dest, quality
            )
        else:
            tmp = dest.with_suffix(".tmp")
            dl_headers = None
            if "anandserver.cfd" in url_or_manifest:
                dl_headers = {
                    "X-API-Key": "ak_8e3f1a7c2b5d9e4f0a6c3b8d1e5f2a9c7b4d0e6f",
                    "api-key": "ak_8e3f1a7c2b5d9e4f0a6c3b8d1e5f2a9c7b4d0e6f"
                }
                
            await self._async_http.stream_to_file(
                url_or_manifest, str(tmp), self._progress_cb, extra_headers=dl_headers
            )
            
            final_dest = await self._mux_audio_async(tmp, dest, quality)
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            return 0, final_dest

    async def _download_from_manifest_async(
        self, manifest_b64: str, dest: Path, quality: str
    ) -> tuple[int, Path]:
        result = parse_manifest(manifest_b64)
        tmp = dest.with_suffix(".tmp")
        try:
            if result.direct_url:
                await self._async_http.stream_to_file(
                    result.direct_url, str(tmp), self._progress_cb
                )
            else:
                await self._download_segments_async(
                    result.init_url, result.media_urls, tmp
                )

            final_dest = await self._mux_audio_async(tmp, dest, quality)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

        return result.sample_rate, final_dest

    async def _download_segments_async(
        self, init_url: str, media_urls: list[str], dest: Path
    ) -> None:
        if aiofiles is None:
            raise RuntimeError(
                "aiofiles non installato — richiesto da TidalProvider._download_segments_async(). "
                "Eseguire: pip install aiofiles"
            )

        dest.parent.mkdir(parents=True, exist_ok=True)
        headers = {"User-Agent": _TIDAL_USER_AGENT}

        total_bytes = 0
        estimated_total = 0
        evt = getattr(self, "_stop_event", None)

        client = await NetworkManager.get_async_client_safe()
        async with aiofiles.open(dest, "wb") as f:
            async with client.stream(
                "GET", init_url, headers=headers, timeout=15
            ) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes():
                    if evt is not None and evt.is_set():
                        raise RuntimeError("Download cancelled by stop_event")
                    if not chunk:
                        continue
                    await f.write(chunk)
                    total_bytes += len(chunk)

            for i, url in enumerate(media_urls):
                if evt is not None and evt.is_set():
                    raise RuntimeError("Download cancelled by stop_event")

                async with client.stream(
                    "GET", url, headers=headers, timeout=15
                ) as resp:
                    resp.raise_for_status()
                    if estimated_total == 0:
                        seg_len = int(resp.headers.get("Content-Length") or 0)
                        if seg_len > 0:
                            estimated_total = seg_len * len(media_urls)
                    async for chunk in resp.aiter_bytes():
                        if evt is not None and evt.is_set():
                            raise RuntimeError("Download cancelled by stop_event")
                        if not chunk:
                            continue
                        await f.write(chunk)
                        total_bytes += len(chunk)

                        if hasattr(self, "_progress_cb") and self._progress_cb:
                            try:
                                self._progress_cb(total_bytes, estimated_total)
                            except TypeError:
                                try:
                                    self._progress_cb(len(chunk))
                                except Exception:
                                    pass

    async def _mux_audio_async(self, src: Path, dst: Path, quality: str) -> Path:
        codec = "flac"

        try:
            rc, stdout, stderr = await self._run_ffprobe(
                "ffprobe",
                "-v",
                "quiet",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(src),
            )
            if rc == 0:
                codec = stdout.strip().lower() or "flac"
            else:
                logger.warning(
                    "[tidal] async ffprobe failed, falling back to quality guess: %s",
                    stderr.strip(),
                )
        except Exception:
            logger.warning(
                "[tidal] async ffprobe failed to detect codec, falling back to quality guess"
            )

        quality_norm = _normalize_quality(quality)
        if codec not in ("flac", "alac"):
            codec = "eac3" if quality_norm == "DOLBY_ATMOS" else codec

        is_lossy = codec not in ("flac", "alac")
        final_dst = dst.with_suffix(".m4a") if is_lossy else dst.with_suffix(".flac")

        cmd = ["ffmpeg", "-y", "-i", str(src), "-vn"]
        cmd.extend(["-c:a", "copy"] if is_lossy else ["-c:a", "flac"])
        cmd.append(str(final_dst))

        rc, stdout, stderr = await self._run_ffmpeg(*cmd)
        if rc != 0:
            failed_file = final_dst.with_suffix(".failed")
            src.rename(failed_file)
            raise SpotiflacError(
                ErrorKind.FILE_IO,
                f"ffmpeg failed (Stream saved as {failed_file.name}): {stderr}",
                "tidal",
            )
        return final_dst

    async def _get_audio_duration_seconds_async(self, file_path: Path) -> int:
        try:
            rc, stdout, stderr = await self._run_ffprobe(
                "ffprobe",
                "-v",
                "quiet",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(file_path),
            )
            if rc == 0:
                return int(float(stdout.strip()))
        except Exception:
            pass
        return 0

    @staticmethod
    def _parse_track_id(tidal_url: str) -> int:
        parts = tidal_url.split("/track/")
        if len(parts) < 2:
            raise ParseError("tidal", f"invalid Tidal URL: {tidal_url}")
        try:
            return int(parts[1].split("?")[0].strip())
        except ValueError as exc:
            raise ParseError("tidal", f"cannot parse track ID from {tidal_url}", exc)

    @staticmethod
    def _random_ua() -> str:
        rng = random.Random()
        rng.seed(int(time.time() // 3600))
        return (
            f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_{rng.randrange(11,15)}_{rng.randrange(4,9)}) "
            f"AppleWebKit/{rng.randrange(530,537)}.{rng.randrange(30,37)} (KHTML, like Gecko) "
            f"Chrome/{rng.randrange(80,105)}.0.{rng.randrange(3000,4500)}.{rng.randrange(60,125)} "
            f"Safari/{rng.randrange(530,537)}.{rng.randrange(30,36)}"
        )

    async def download_track_async(
        self,
        metadata: TrackMetadata,
        output_dir: str,
        *,
        filename_format: str = "{title} - {artist}",
        position: int = 1,
        include_track_num: bool = False,
        use_album_track_num: bool = False,
        first_artist_only: bool = False,
        allow_fallback: bool = True,
        quality: str = "LOSSLESS",
        embed_lyrics: bool = False,
        lyrics_providers: list[str] | None = None,
        enrich_metadata: bool = False,
        enrich_providers: list[str] | None = None,
        is_album: bool = False,
        **kwargs: Any,
    ) -> DownloadResult:
        try:
            from types import SimpleNamespace

            if isinstance(metadata, (int, str)):
                try:
                    numeric = int(metadata)
                    metadata = SimpleNamespace(
                        id=f"tidal_{numeric}",
                        title="",
                        artists="",
                        isrc="",
                        duration_ms=0,
                        cover_url=None,
                    )
                except Exception:
                    metadata = SimpleNamespace(
                        id=str(metadata),
                        title="",
                        artists="",
                        isrc="",
                        duration_ms=0,
                        cover_url=None,
                    )

            meta_id = getattr(metadata, "id", "")
            if meta_id and str(meta_id).startswith("tidal_"):
                tidal_url = f"https://listen.tidal.com/track/{str(meta_id).removeprefix('tidal_')}"
                logger.info("[tidal] Direct Tidal ID detected: %s", meta_id)
            else:
                tidal_url = await self.resolve_spotify_to_tidal_async(
                    getattr(metadata, "id", ""),
                    getattr(metadata, "title", ""),
                    getattr(metadata, "artists", ""),
                    getattr(metadata, "isrc", ""),
                    getattr(metadata, "duration_ms", 0),
                )
            track_id = self._parse_track_id(tidal_url)

            tidal_tags: dict[str, str] = {}

            # Qobuz (per ISRC) e il proxy Tidal (per releaseDate, e ISRC come
            # ultima risorsa) sono richieste di rete indipendenti tra loro:
            # le eseguiamo in parallelo con asyncio.gather invece di awaitarle
            # in sequenza, per ridurre la latenza totale di questa fase.
            qobuz_isrc, details = await asyncio.gather(
                _find_isrc_via_qobuz(
                    getattr(metadata, "title", ""),
                    getattr(metadata, "artists", ""),
                    getattr(metadata, "duration_ms", 0),
                ),
                self._fetch_track_details_from_proxy(track_id),
                return_exceptions=True,
            )

            # asyncio.gather con return_exceptions=True non propaga le
            # exceptions: normalize them here to "no results", so
            # un fallimento di una delle due chiamate non comporta la perdita
            # dell'altra (comportamento equivalente a due try/except separati).
            if isinstance(qobuz_isrc, BaseException):
                logger.debug("[tidal] Qobuz ISRC lookup raised: %s", qobuz_isrc)
                qobuz_isrc = None
            if isinstance(details, BaseException):
                logger.debug("[tidal] proxy track-details lookup raised: %s", details)
                details = {}

            # Qobuz has priority over Spotify: always consulted,
            # il suo ISRC sovrascrive quello originale se trovato.
            if qobuz_isrc:
                metadata.isrc = qobuz_isrc
                tidal_tags["ISRC"] = qobuz_isrc
                logger.info("[tidal] ISRC from Qobuz (preferred): %s", qobuz_isrc)
            elif metadata.isrc:
                tidal_tags["ISRC"] = metadata.isrc
                logger.info("[tidal] ISRC from source metadata: %s", metadata.isrc)

            # Proxy used for releaseDate; its ISRC is only the last resort.
            if details:
                if not metadata.isrc:
                    if isrc_from_proxy := details.get("isrc"):
                        tidal_tags["ISRC"] = isrc_from_proxy
                        metadata.isrc = isrc_from_proxy
                        logger.info(
                            "[tidal] ISRC from proxy (last resort): %s", isrc_from_proxy
                        )

                if rd := details.get("releaseDate"):
                    if len(rd) >= 4:
                        tidal_tags["ORIGINALDATE"] = rd
                        tidal_tags["ORIGINALYEAR"] = rd[:4]

            # ----------------------------------------------------------------
            # MUSICBRAINZ
            # ----------------------------------------------------------------
            mb_tags: dict[str, str] = {}
            if metadata.isrc:
                mb_data = await fetch_mb_metadata_async(metadata.isrc)
                mb_tags = mb_result_to_tags(mb_data)

            # ----------------------------------------------------------------
            # Final merge of tags (Tidal base, MusicBrainz higher priority)
            # ----------------------------------------------------------------
            combined_tags = {**tidal_tags, **mb_tags}

            dest = await asyncio.to_thread(
                self._build_output_path,
                metadata,
                output_dir,
                filename_format,
                position,
                include_track_num,
                use_album_track_num,
                first_artist_only,
            )
            if self._file_exists(dest) or self._file_exists(dest.with_suffix(".m4a")):
                existing_path = (
                    dest if self._file_exists(dest) else dest.with_suffix(".m4a")
                )
                return DownloadResult.skipped_result(self.name, str(existing_path))

            dl_url = (
                await self._get_download_url_with_fallback_async(track_id, quality)
                if allow_fallback
                else await self._get_download_url_async(track_id, quality)
            )

            sample_rate, final_dest = await self._download_file_async(
                dl_url, dest, quality
            )

            if sample_rate > 0:
                logger.info(
                    "[tidal] Extracted true sample rate from manifest: %d Hz",
                    sample_rate,
                )

            expected_s = metadata.duration_ms // 1000

            valid, err_msg = await validate_downloaded_track_async(
                str(final_dest), expected_s
            )
            if not valid:
                raise SpotiflacError(ErrorKind.UNAVAILABLE, err_msg, self.name)

            # Controllo Preview derivato dal Web JS
            actual_s = await self._get_audio_duration_seconds_async(final_dest)
            if actual_s <= 35 and expected_s > 45:
                # E' probabile che sia stato scaricato un preview limitato
                if final_dest.exists():
                    final_dest.unlink()
                raise SpotiflacError(
                    ErrorKind.UNAVAILABLE,
                    f"Tidal returned a limited preview track ({actual_s}s).",
                    self.name,
                )

            # Validate and repair FLAC files if needed
            if str(final_dest).lower().endswith(".flac"):
                success, repair_msg = await asyncio.to_thread(
                    validate_and_repair_if_needed, str(final_dest)
                )
                if not success:
                    logger.error("[tidal] FLAC file validation failed: %s", repair_msg)
                    if final_dest.exists():
                        final_dest.unlink()
                    raise SpotiflacError(
                        ErrorKind.UNAVAILABLE,
                        f"FLAC validation failed: {repair_msg}",
                        self.name,
                    )
                if repair_msg:
                    logger.info("[tidal] FLAC file repair status: %s", repair_msg)

            sample_rate_tag = {}
            if sample_rate > 0:
                logger.info("✦ Tidal source sample rate: %d Hz", sample_rate)
                sample_rate_tag["SAMPLERATE"] = str(sample_rate)

            _print_mb_summary(mb_tags)

            opts = EmbedOptions(
                first_artist_only=first_artist_only,
                cover_url=metadata.cover_url,
                extra_tags={**combined_tags, **sample_rate_tag},
                embed_lyrics=embed_lyrics,
                lyrics_providers=lyrics_providers or [],
                enrich=enrich_metadata,
                enrich_providers=enrich_providers,
                enrich_qobuz_token=self._qobuz_token or "",
                is_album=is_album,
            )

            await embed_metadata_async(str(final_dest), metadata, opts)
            return DownloadResult.ok(self.name, str(final_dest))
        except SpotiflacError as exc:
            logger.error("[tidal] %s", exc)
            return DownloadResult.fail(self.name, str(exc))
        except Exception as exc:
            logger.exception("[tidal] unexpected error")
            return DownloadResult.fail(self.name, str(exc))
