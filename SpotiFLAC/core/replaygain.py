"""SpotiFLAC/core/replaygain.py — how loud is this track, really?

Tracks come from different masters, different eras and different providers,
and their loudness varies by twenty decibels or more. ReplayGain is the
long-standing answer: measure the track once, write the correction into its
tags, and let the player apply it. Every serious player reads these tags —
foobar2000, VLC, Rekordbox, Plexamp, Kodi — and a library without them
plays as an argument between a 1970s master and a modern one.

Measurement is ffmpeg's `loudnorm` filter in analysis mode, which reports
integrated loudness in LUFS and true peak in dBTP against EBU R128. Ported
from the desktop app's backend/replaygain.go, including its -18 LUFS
reference: that is the ReplayGain 2.0 convention, and matching it is what
makes these tags mean the same thing to a player as anyone else's.

Analysis decodes the whole file, so it costs real time — a few seconds per
track on this machine. That is why it is opt-in rather than part of every
download.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: ReplayGain 2.0's reference level. A track measured at exactly this needs
#: no correction; everything else is scaled to meet it.
REFERENCE_LUFS = -18.0

#: loudnorm reports this when it cannot measure loudness at all — silence,
#: or a file short enough that the gating never fills. Treated as "no
#: answer" rather than as a real and absurd measurement.
_UNMEASURABLE_LUFS = -70.0

_JSON_BLOCK = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.DOTALL)


@dataclass(frozen=True)
class ReplayGainResult:
    """One track's measurement, in the units the tags use."""

    #: dB of correction to reach the reference. Positive means turn it up.
    track_gain_db: float
    #: Peak as a linear sample value, where 1.0 is full scale. Written as a
    #: number rather than dB because that is what the tag convention says,
    #: and players use it to avoid clipping when applying the gain.
    track_peak: float
    #: Measured integrated loudness, kept so the tags can say what they were
    #: measured against.
    input_lufs: float

    def as_tags(self) -> dict[str, str]:
        """The Vorbis/ID3 tag names players actually look for.

        The formatting matters: players parse these as text, and the
        conventional forms are "-3.42 dB" and a bare float for the peak.
        """
        return {
            "REPLAYGAIN_TRACK_GAIN": f"{self.track_gain_db:.2f} dB",
            "REPLAYGAIN_TRACK_PEAK": f"{self.track_peak:.6f}",
            "REPLAYGAIN_REFERENCE_LOUDNESS": f"{REFERENCE_LUFS:.2f} LUFS",
        }


def _db_to_linear(db: float) -> float:
    return float(10.0 ** (db / 20.0))


def _parse_loudnorm(output: str) -> ReplayGainResult | None:
    """Reads loudnorm's JSON summary out of ffmpeg's stderr.

    ffmpeg writes the block among its ordinary progress chatter rather than
    on its own, so it is extracted by pattern instead of by parsing the
    whole stream as JSON.
    """
    match = _JSON_BLOCK.search(output)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        input_i = float(data["input_i"])
        input_tp = float(data["input_tp"])
    except (ValueError, KeyError, TypeError) as exc:
        logger.debug("[replaygain] could not read loudnorm output: %s", exc)
        return None

    if not math.isfinite(input_i) or input_i <= _UNMEASURABLE_LUFS:
        logger.debug("[replaygain] no measurable loudness (input_i=%s)", input_i)
        return None

    peak = _db_to_linear(input_tp) if math.isfinite(input_tp) else 1.0
    return ReplayGainResult(
        track_gain_db=REFERENCE_LUFS - input_i,
        # A true peak above full scale is real and common in loud masters;
        # clamping it would tell the player there is headroom that is not
        # there, which is the one thing the peak tag exists to prevent.
        track_peak=max(peak, 0.0),
        input_lufs=input_i,
    )


async def analyse_async(
    file_path: str | Path, *, timeout_s: float = 300.0
) -> ReplayGainResult | None:
    """Measures one file. Returns None rather than raising, for any reason.

    A missing ffmpeg, an undecodable file or a track too quiet to gate all
    mean the same thing to the caller — no tags to write — and none of them
    should fail a download that otherwise succeeded.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        logger.debug("[replaygain] ffmpeg not on PATH; skipping analysis")
        return None

    args = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-i",
        str(file_path),
        "-af",
        f"loudnorm=I={REFERENCE_LUFS}:TP=-1:LRA=11:print_format=json",
        "-f",
        "null",
        "-",
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            logger.debug("[replaygain] analysis timed out for %s", file_path)
            return None
    except Exception as exc:
        logger.debug("[replaygain] could not run ffmpeg for %s: %s", file_path, exc)
        return None

    return _parse_loudnorm(stderr.decode("utf-8", "replace"))


async def replaygain_tags_async(file_path: str | Path) -> dict[str, str]:
    """The tags for one file, or {} if it could not be measured.

    Shaped for EmbedOptions.extra_tags, so a caller merges the result in and
    the existing tagger writes it for every format it already supports.
    """
    result = await analyse_async(file_path)
    return result.as_tags() if result else {}
