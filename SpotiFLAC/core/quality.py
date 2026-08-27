"""Centralized quality normalization and provider mappings."""

from __future__ import annotations

# Canonical qualities
_CANONICAL = {
    "HI_RES_LOSSLESS": ["27", "HI_RES_LOSSLESS", "HI-RES-LOSSLESS", "HIRES_LOSSLESS"],
    "HI_RES": ["7", "HI_RES", "HIRES", "HI-RES"],
    "LOSSLESS": ["6", "LOSSLESS"],
    "HIGH": ["5", "HIGH"],
    "LOW": ["4", "LOW"],
    "DOLBY_ATMOS": ["DOLBY_ATMOS", "ATMOS", "DOLBY", "EAC3", "EC3", "EAC3_JOC"],
}

_LOSSLESS_PROVIDERS = {"tidal", "qobuz", "amazon", "apple", "deezer"}


def normalize_quality(q: str) -> str:
    """Return a canonical quality name for a provider-agnostic input."""
    if not q:
        return "LOSSLESS"
    s = str(q).strip().upper()
    for canon, aliases in _CANONICAL.items():
        if s in aliases or s == canon:
            return canon
    # fallback heuristics
    if s.isdigit():
        if s == "27":
            return "HI_RES_LOSSLESS"
        if s == "7":
            return "HI_RES"
        if s == "6":
            return "LOSSLESS"
    if "HI" in s or "24" in s or "96" in s:
        return "HI_RES_LOSSLESS"
    if "LOSS" in s:
        return "LOSSLESS"
    if "LOW" in s or "MP3" in s:
        return "LOW"
    return "LOSSLESS"


def quality_fallback_chain(quality: str) -> list[str]:
    """Return a canonical fallback chain for a given quality."""
    chains = {
        "DOLBY_ATMOS": ["DOLBY_ATMOS", "HI_RES_LOSSLESS", "LOSSLESS"],
        "HI_RES_LOSSLESS": ["HI_RES_LOSSLESS", "LOSSLESS"],
        "HI_RES": ["HI_RES", "LOSSLESS"],
        "LOSSLESS": ["LOSSLESS"],
        "HIGH": ["LOSSLESS"],
        "LOW": ["LOSSLESS"],
    }
    n = normalize_quality(quality)
    return chains.get(n, [n or "LOSSLESS"])


def quality_for_provider(provider: str, quality: str) -> str:
    """Translate a canonical quality into the provider's native token.

    DOLBY_ATMOS is a Tidal-exclusive tier: no other provider actually
    serves an Atmos stream, so a DOLBY_ATMOS request against anything but
    Tidal is treated as HI_RES_LOSSLESS instead — the best lossless tier
    that provider *does* have — rather than being passed through as a
    token the provider wouldn't understand.
    """
    normalized = normalize_quality(quality)
    name = str(provider or "").lower().replace("ext:", "")
    for suffix in ("-native", "-web", "-py", "_native", "_web", "_py"):
        name = name.removesuffix(suffix)

    if normalized == "DOLBY_ATMOS" and name != "tidal":
        normalized = "HI_RES_LOSSLESS"

    if name == "qobuz":
        if normalized == "HI_RES":
            return "7"
        return "27" if normalized == "HI_RES_LOSSLESS" else "6"
    if name == "tidal":
        return normalized
    if name in _LOSSLESS_PROVIDERS:
        if name == "amazon":
            return "best"
        if name == "apple":
            return "ALAC"
        return "FLAC"
    if name == "pandora":
        return "mp3_192"
    if name in {"youtube", "ytmusic"}:
        return "best"
    if name == "soundcloud":
        return "mp3_128"
    return normalized


def get_squid_tier(q: str) -> str:
    """Return Squid 'tier' value for a given quality ("best" or "hd")."""
    n = normalize_quality(q)
    return "best" if n in ("HI_RES_LOSSLESS", "HI_RES") else "hd"


def to_zarz_codec(q: str) -> str:
    """Map normalized quality to zarz codec parameter (conservative defaults)."""
    n = normalize_quality(q)
    if n == "HI_RES_LOSSLESS":
        return "flac"
    if n == "HI_RES":
        return "flac"
    if n == "LOSSLESS":
        return "flac"
    # For other cases prefer mp4/mp3-like container
    return "mp4"


def map_musicdl_quality(q: str) -> str:
    """Map generic quality to MusicDL 'quality' strings used by zarz endpoints."""
    n = normalize_quality(q)
    if n == "HI_RES_LOSSLESS":
        return "hi-res-max"
    if n == "HI_RES":
        return "hi-res"
    return "cd"


def map_amazon_community_quality(q: str) -> str:
    """Map generic quality to Amazon Community 'quality' strings (16, 24).

    Amazon never gets "atmos" here — Dolby Atmos is Tidal-exclusive (see
    quality_for_provider()) — so a DOLBY_ATMOS input is treated the same
    as HI_RES_LOSSLESS/HI_RES, same as everywhere else that isn't Tidal.
    """
    n = normalize_quality(q)
    if n in ("LOSSLESS", "HIGH", "LOW"):
        return "16"
    # Covers HI_RES_LOSSLESS, HI_RES, and DOLBY_ATMOS (Tidal-only elsewhere)
    return "24"
