"""
Centralized quality normalization and provider mappings.
"""
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
        return "HI_RES"
    if "LOSS" in s:
        return "LOSSLESS"
    if "LOW" in s or "MP3" in s:
        return "LOW"
    return "LOSSLESS"


def quality_fallback_chain(quality: str) -> list[str]:
    """Return a canonical fallback chain for a given quality."""
    chains = {
        "DOLBY_ATMOS":    ["DOLBY_ATMOS", "HI_RES_LOSSLESS", "LOSSLESS", "HIGH", "LOW"],
        "HI_RES_LOSSLESS": ["HI_RES_LOSSLESS", "LOSSLESS", "HIGH", "LOW"],
        "LOSSLESS":        ["LOSSLESS", "HIGH", "LOW"],
        "HIGH":            ["HIGH", "LOW"],
        "LOW":             ["LOW"],
    }
    n = normalize_quality(quality)
    return chains.get(n, [n or "LOSSLESS"])


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
