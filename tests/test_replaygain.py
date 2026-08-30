"""Measuring how loud a track is, so a player can even the library out.

Tracks come from different masters, eras and providers, and their loudness
varies by twenty decibels or more. ReplayGain is the long-standing fix, and
the numbers only mean anything if they follow the convention: gain measured
against -18 LUFS, peak as a linear sample value where 1.0 is full scale.
Getting either wrong produces tags a player will happily apply and make
things worse with.

The arithmetic is checked against fixed loudnorm output rather than real
audio, so the tests are fast and deterministic; one end-to-end case runs the
real ffmpeg when it is available.
"""

from __future__ import annotations

import asyncio
import shutil

import pytest

from SpotiFLAC.core.replaygain import (
    REFERENCE_LUFS,
    ReplayGainResult,
    _parse_loudnorm,
    analyse_async,
)


def _loudnorm(input_i: str, input_tp: str = "-1.00") -> str:
    """ffmpeg writes this among its ordinary progress chatter, not alone."""
    return (
        "frame=  1 fps=0.0 q=-0.0 size=N/A time=00:00:10.00 speed=38x\n"
        "[Parsed_loudnorm_0 @ 0x7f8] \n"
        "{\n"
        f'\t"input_i" : "{input_i}",\n'
        f'\t"input_tp" : "{input_tp}",\n'
        '\t"input_lra" : "3.20",\n'
        '\t"output_i" : "-18.06",\n'
        '\t"normalization_type" : "dynamic"\n'
        "}\n"
        "[out#0/null @ 0x7f8] video:0KiB audio:14KiB\n"
    )


# --- the arithmetic --------------------------------------------------------


def test_a_loud_master_is_told_to_turn_down() -> None:
    """-8.77 LUFS is nine decibels above the reference, so the gain is
    negative — measured from a real modern master.
    """
    result = _parse_loudnorm(_loudnorm("-8.77", "1.28"))
    assert result.track_gain_db == pytest.approx(-9.23, abs=0.01)


def test_a_quiet_master_is_told_to_turn_up() -> None:
    result = _parse_loudnorm(_loudnorm("-24.00"))
    assert result.track_gain_db == pytest.approx(6.0, abs=0.01)


def test_a_track_already_at_the_reference_needs_no_correction() -> None:
    result = _parse_loudnorm(_loudnorm(f"{REFERENCE_LUFS:.2f}"))
    assert result.track_gain_db == pytest.approx(0.0, abs=0.01)


def test_a_peak_above_full_scale_is_reported_not_clamped() -> None:
    """True peaks over 0 dBTP are real and common in loud masters. Clamping
    to 1.0 would tell the player there is headroom that is not there, which
    is the one thing the peak tag exists to prevent.
    """
    result = _parse_loudnorm(_loudnorm("-8.77", "1.28"))
    assert result.track_peak > 1.0
    assert result.track_peak == pytest.approx(1.1588, abs=0.001)


def test_the_peak_is_linear_not_decibels() -> None:
    """-6 dBTP is half of full scale."""
    result = _parse_loudnorm(_loudnorm("-18.00", "-6.02"))
    assert result.track_peak == pytest.approx(0.5, abs=0.001)


# --- what must not produce tags -------------------------------------------


def test_silence_is_not_measured() -> None:
    """loudnorm reports -70 or below when gating never fills. Writing a
    +52 dB correction from that would be catastrophic on playback.
    """
    assert _parse_loudnorm(_loudnorm("-70.00")) is None
    assert _parse_loudnorm(_loudnorm("-91.00")) is None


@pytest.mark.parametrize(
    "output",
    ["", "ffmpeg: no such file", "{}", '{"input_i" : "not a number"}'],
)
def test_unreadable_output_yields_nothing(output) -> None:
    assert _parse_loudnorm(output) is None


def test_a_missing_ffmpeg_is_not_an_error(monkeypatch, tmp_path) -> None:
    """A track with no ReplayGain tags plays; a download that failed because
    ffmpeg was absent does not.
    """
    monkeypatch.setattr("SpotiFLAC.core.replaygain.shutil.which", lambda _n: None)
    assert asyncio.run(analyse_async(tmp_path / "x.flac")) is None


def test_an_undecodable_file_is_not_an_error(tmp_path) -> None:
    if not shutil.which("ffmpeg"):
        pytest.skip("needs ffmpeg")
    junk = tmp_path / "not-audio.flac"
    junk.write_bytes(b"this is not audio")
    assert asyncio.run(analyse_async(junk)) is None


# --- the tags themselves ---------------------------------------------------


def test_the_tag_names_and_formats_are_the_conventional_ones() -> None:
    """Players parse these as text. "-9.23 dB" and a bare float are what
    every other tagger writes, and matching them is the whole point.
    """
    tags = ReplayGainResult(
        track_gain_db=-9.23, track_peak=1.158777, input_lufs=-8.77
    ).as_tags()
    assert tags["REPLAYGAIN_TRACK_GAIN"] == "-9.23 dB"
    assert tags["REPLAYGAIN_TRACK_PEAK"] == "1.158777"
    assert tags["REPLAYGAIN_REFERENCE_LOUDNESS"] == "-18.00 LUFS"


# --- end to end ------------------------------------------------------------


def test_real_audio_is_measured_and_tagged(tmp_path) -> None:
    if not shutil.which("ffmpeg"):
        pytest.skip("needs ffmpeg")

    import subprocess

    from mutagen.flac import FLAC

    from SpotiFLAC.core.models import TrackMetadata
    from SpotiFLAC.core.tagger import EmbedOptions, embed_metadata_async

    path = tmp_path / "tone.flac"
    subprocess.run(
        [
            "ffmpeg",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=5",
            "-y",
            str(path),
            "-loglevel",
            "error",
        ],
        check=True,
    )

    metadata = TrackMetadata(
        id="0" * 22, title="t", artists="a", album="", album_artist="a"
    )
    asyncio.run(embed_metadata_async(path, metadata, EmbedOptions(replaygain=True)))

    written = {k.upper(): v for k, v in FLAC(str(path)).items()}
    assert "REPLAYGAIN_TRACK_GAIN" in written
    assert written["REPLAYGAIN_TRACK_GAIN"][0].endswith(" dB")


def test_replaygain_is_off_unless_asked_for(tmp_path) -> None:
    """The analysis decodes the whole file. Nobody should pay for it by
    default.
    """
    from SpotiFLAC.core.tagger import EmbedOptions

    assert EmbedOptions().replaygain is False
