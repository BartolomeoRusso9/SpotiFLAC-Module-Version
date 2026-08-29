"""core/library_upgrade.py — finding the files in a library that could be better.

Every piece of this already existed and none of them were connected:

  - `local_scanner` reads a file's tags, so we know what a file *is*.
  - `quality` knows the canonical tiers and how to normalise a name onto them.
  - `hires_check` can tell a genuine 24/96 master from one upsampled off a CD.
  - `SpotiflacDownloader` can fetch a track given its Spotify URL.

What was missing is the pass that walks a folder, works out which files are
below the quality you actually want, and hands that list to the downloader.
That is the whole of this module: a *classifier* and a *plan*. It never
downloads and never deletes — the CLI (tools/library_upgrade_cli.py) and the
ordinary download path do that, which is what keeps this testable with a
handful of files and no network.

Tiers
-----
Three, deliberately coarse, because they are the three that change what you
hear:

  1. `lossy`    — MP3, AAC, Opus, Vorbis, WMA. Information is gone.
  2. `lossless` — FLAC/ALAC/WAV at up to 16-bit / 48 kHz. CD quality.
  3. `hires`    — lossless above either of those limits.

A file is a candidate when its tier is below the target's. Sample rate and
bit depth are read from the container, so this costs a header read per file
rather than a decode.

Fake hi-res
-----------
A file can *declare* 24/96 and contain nothing above 22 kHz — see
`hires_check`. With `verify=True` such a file is reclassified down to the
tier its content actually justifies, which is the only way "upgrade my
library to hi-res" gives an honest answer. It is off by default because it
decodes 30 seconds of audio per file, and on a large library that is the
difference between a scan of seconds and one of hours.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .quality import normalize_quality
from .tagger import SUPPORTED_SUFFIXES

logger = logging.getLogger(__name__)

TIER_LOSSY = 1
TIER_LOSSLESS = 2
TIER_HIRES = 3

TIER_NAMES = {TIER_LOSSY: "lossy", TIER_LOSSLESS: "lossless", TIER_HIRES: "hires"}

#: Containers that are always lossy, whatever their headers say.
LOSSY_SUFFIXES = frozenset({".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wma"})

#: Above either of these, a lossless file counts as hi-res.
CD_SAMPLE_RATE_MAX = 48000
CD_BIT_DEPTH_MAX = 16


def target_tier(quality: str) -> int:
    """The tier a canonical quality name asks for."""
    canonical = normalize_quality(quality)
    if canonical in ("HI_RES_LOSSLESS", "HI_RES", "DOLBY_ATMOS"):
        return TIER_HIRES
    if canonical == "LOSSLESS":
        return TIER_LOSSLESS
    return TIER_LOSSY


@dataclass
class AudioQuality:
    """What a file's own headers say about it."""

    file_path: str
    codec: str = ""
    sample_rate: int = 0
    bits_per_sample: int = 0
    bitrate: int = 0
    channels: int = 0
    lossless: bool = False
    error: str = ""

    @property
    def tier(self) -> int:
        if not self.lossless:
            return TIER_LOSSY
        if (
            self.sample_rate > CD_SAMPLE_RATE_MAX
            or self.bits_per_sample > CD_BIT_DEPTH_MAX
        ):
            return TIER_HIRES
        return TIER_LOSSLESS

    @property
    def tier_name(self) -> str:
        return TIER_NAMES.get(self.tier, "unknown")

    def describe(self) -> str:
        if self.error:
            return f"unreadable ({self.error})"
        parts = [self.codec or "?"]
        if self.bits_per_sample:
            parts.append(f"{self.bits_per_sample}-bit")
        if self.sample_rate:
            parts.append(f"{self.sample_rate / 1000:g} kHz")
        if not self.lossless and self.bitrate:
            parts.append(f"{round(self.bitrate / 1000)} kbps")
        return " · ".join(parts)


def inspect_file(path: str | Path) -> AudioQuality:
    """Reads one file's audio properties. Never raises.

    A file that cannot be read comes back with `.error` set, so a scan of a
    thousand files is not aborted by one that is truncated — the same
    contract `local_scanner.scan_file()` has.
    """
    p = Path(path)
    quality = AudioQuality(file_path=str(p))

    if not p.exists():
        quality.error = "File not found"
        return quality

    suffix = p.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        quality.error = f"Unsupported format: {suffix}"
        return quality

    try:
        import mutagen

        audio = mutagen.File(str(p))
    except Exception as exc:
        quality.error = f"Could not read: {exc}"
        return quality

    if audio is None or getattr(audio, "info", None) is None:
        quality.error = "No audio stream found"
        return quality

    info = audio.info
    # mutagen names the codec in `info.codec` for containers that can hold
    # more than one (MP4/M4A); elsewhere the implementing module is the
    # clearest label there is.
    quality.codec = str(
        getattr(info, "codec", "") or type(info).__module__.rsplit(".", 1)[-1]
    )
    quality.sample_rate = int(getattr(info, "sample_rate", 0) or 0)
    quality.bitrate = int(getattr(info, "bitrate", 0) or 0)
    quality.channels = int(getattr(info, "channels", 0) or 0)
    quality.bits_per_sample = int(getattr(info, "bits_per_sample", 0) or 0)

    quality.lossless = _is_lossless(suffix, quality.codec)

    if not quality.lossless:
        # mutagen reports bits_per_sample=16 for AAC, which is a property of
        # the decoder's output rather than of the stored audio. Reporting it
        # would put "16-bit" next to a 128 kbps file in describe(), which is
        # true of nothing anyone cares about.
        quality.bits_per_sample = 0

    return quality


def _is_lossless(suffix: str, codec: str) -> bool:
    """Whether this container/codec pair preserves the original samples.

    The container alone is not the answer: `.m4a` holds both AAC (lossy) and
    ALAC (lossless), and no amount of looking at the bitrate distinguishes
    them reliably. Where a codec name is available it decides; otherwise the
    suffix does.
    """
    codec = (codec or "").lower()
    if suffix in (".m4a", ".mp4", ".m4b"):
        return "alac" in codec
    if suffix in (".ogg", ".oga"):
        # Ogg usually carries Vorbis, but Ogg FLAC exists and is lossless.
        return "flac" in codec
    return suffix not in LOSSY_SUFFIXES


@dataclass
class UpgradeCandidate:
    """One file that is below the requested tier, and what it is."""

    file_path: str
    quality: AudioQuality
    title: str = ""
    artist: str = ""
    album: str = ""
    isrc: str = ""
    current_tier: int = TIER_LOSSY
    target_tier: int = TIER_LOSSLESS
    reason: str = ""

    @property
    def search_query(self) -> str:
        """What to look this track up by, when there is no ISRC."""
        return " ".join(part for part in (self.artist, self.title) if part).strip()

    def to_dict(self) -> dict:
        return {
            "file_path": self.file_path,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "isrc": self.isrc,
            "current": self.quality.describe(),
            "current_tier": TIER_NAMES.get(self.current_tier, "unknown"),
            "target_tier": TIER_NAMES.get(self.target_tier, "unknown"),
            "reason": self.reason,
        }


@dataclass
class ScanReport:
    target: str
    scanned: int = 0
    unreadable: int = 0
    already_ok: int = 0
    candidates: list[UpgradeCandidate] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "scanned": self.scanned,
            "unreadable": self.unreadable,
            "already_ok": self.already_ok,
            "upgradable": len(self.candidates),
            "candidates": [c.to_dict() for c in self.candidates],
        }

    def summary(self) -> str:
        lines = [
            f"Scanned {self.scanned} file(s) against target '{self.target}':",
            f"  already at or above target : {self.already_ok}",
            f"  could be upgraded          : {len(self.candidates)}",
        ]
        if self.unreadable:
            lines.append(f"  unreadable                 : {self.unreadable}")
        return "\n".join(lines)


def iter_audio_files(root: str | Path, *, recursive: bool = True) -> list[Path]:
    p = Path(root)
    if p.is_file():
        return [p] if p.suffix.lower() in SUPPORTED_SUFFIXES else []
    if not p.is_dir():
        return []
    pattern = "**/*" if recursive else "*"
    return sorted(
        f
        for f in p.glob(pattern)
        if f.is_file() and f.suffix.lower() in SUPPORTED_SUFFIXES
    )


def scan_library(
    root: str | Path,
    target_quality: str = "LOSSLESS",
    *,
    recursive: bool = True,
    verify_hires: bool = False,
    progress: Any = None,
) -> ScanReport:
    """Walks `root` and returns everything below `target_quality`.

    `progress(done, total, path)` is called per file if given, so a CLI or a
    UI can show something during what is, on a real library, a long walk.
    """
    from .local_scanner import scan_file

    target = target_tier(target_quality)
    report = ScanReport(target=normalize_quality(target_quality))
    files = iter_audio_files(root, recursive=recursive)

    for index, path in enumerate(files, start=1):
        report.scanned += 1
        if progress is not None:
            try:
                progress(index, len(files), str(path))
            except Exception:
                # A progress callback is a convenience; it must not be able to
                # abort a scan that is otherwise working.
                logger.debug("[upgrade] progress callback raised", exc_info=True)

        quality = inspect_file(path)
        if quality.error:
            report.unreadable += 1
            continue

        current = quality.tier
        reason = ""

        if verify_hires and current == TIER_HIRES:
            verdict = _verify_hires(path)
            if verdict == "fake_hires":
                # It claims hi-res and isn't. Its honest tier is CD at best.
                current = TIER_LOSSLESS
                reason = "declares Hi-Res but has a CD-range spectral cutoff"

        if current >= target:
            report.already_ok += 1
            continue

        if not reason:
            reason = (
                f"{TIER_NAMES.get(current, 'unknown')} "
                f"below target {TIER_NAMES.get(target, 'unknown')}"
            )

        info = scan_file(path)
        report.candidates.append(
            UpgradeCandidate(
                file_path=str(path),
                quality=quality,
                title=info.search_title,
                artist=info.search_artist,
                album=info.old_album,
                isrc=info.old_isrc,
                current_tier=current,
                target_tier=target,
                reason=reason,
            )
        )

    return report


def _verify_hires(path: Path) -> str:
    """hires_check's verdict for one file, or "" when it can't run.

    Wrapped because the check is an optional dependency (`SpotiFLAC[hires]`)
    and a missing librosa must degrade to "don't reclassify" rather than
    failing the scan.
    """
    try:
        from .hires_check import check_file, is_available

        if not is_available():
            logger.warning(
                "[upgrade] --verify-hires needs the optional 'hires' extra "
                "(pip install 'SpotiFLAC[hires]'); scanning without it."
            )
            return ""
        return check_file(path).verdict
    except Exception as exc:
        logger.debug("[upgrade] hires check failed for %s: %s", path, exc)
        return ""


# ─────────────────────────────────────────────────────────────
#  Turning candidates into something downloadable
# ─────────────────────────────────────────────────────────────


async def resolve_candidate_async(
    candidate: UpgradeCandidate,
    *,
    client: Any = None,
) -> str:
    """The Spotify track URL for a candidate, or "" if it can't be found.

    ISRC first, because it identifies a *recording* and survives every
    retagging and rename; text search is the fallback for the files that have
    no ISRC, which are exactly the badly-tagged ones this feature exists to
    improve.
    """
    from .spotify_metadata import SpotifyMetadataClient

    metadata_client = client or SpotifyMetadataClient()

    if candidate.isrc:
        try:
            results = await metadata_client.search_async(
                f"isrc:{candidate.isrc}", limit=1
            )
            tracks = results.get("tracks") or []
            if tracks:
                return _track_url(tracks[0])
        except Exception:
            logger.debug("[upgrade] ISRC lookup failed for %s", candidate.isrc)

    query = candidate.search_query
    if not query:
        return ""
    try:
        results = await metadata_client.search_async(query, limit=1)
        tracks = results.get("tracks") or []
        return _track_url(tracks[0]) if tracks else ""
    except Exception:
        logger.debug("[upgrade] search failed for %r", query)
        return ""


def _track_url(track: Any) -> str:
    for attr in ("external_url", "url"):
        value = getattr(track, attr, None) or (
            track.get(attr) if isinstance(track, dict) else None
        )
        if value:
            return str(value)
    track_id = getattr(track, "id", None) or (
        track.get("id") if isinstance(track, dict) else None
    )
    return f"https://open.spotify.com/track/{track_id}" if track_id else ""


async def plan_async(
    report: ScanReport,
    *,
    client: Any = None,
    limit: int | None = None,
) -> list[tuple[UpgradeCandidate, str]]:
    """Pairs each candidate with the URL to re-fetch it from.

    Candidates that cannot be resolved are returned with an empty URL rather
    than dropped: "we could not find this one on Spotify" is a result the
    person running an upgrade needs to see, not something to hide.
    """
    from .spotify_metadata import SpotifyMetadataClient

    metadata_client = client or SpotifyMetadataClient()
    candidates = report.candidates[:limit] if limit else report.candidates

    pairs: list[tuple[UpgradeCandidate, str]] = []
    for candidate in candidates:
        url = await resolve_candidate_async(candidate, client=metadata_client)
        pairs.append((candidate, url))
        # One at a time, and briefly: this is a search per file, and a
        # library upgrade can easily be a thousand of them.
        await asyncio.sleep(0)
    return pairs
