"""Lossless transcode targets: format table, ffmpeg commands, real round-trips.

The point of a lossless target is that the decoded samples survive the
conversion untouched, so the checks that matter here are (a) the command
never asks ffmpeg to resample or re-quantise and (b) a real conversion
decodes back to byte-identical PCM.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import shutil
import subprocess
from pathlib import Path

import pytest

from SpotiFLAC.core import tagger, transcode
from SpotiFLAC.core.errors import SpotiflacError

_HAS_FFMPEG = shutil.which("ffmpeg") is not None
needs_ffmpeg = pytest.mark.skipif(_HAS_FFMPEG is False, reason="ffmpeg not installed")


# ---------------------------------------------------------------------------
# Format table
# ---------------------------------------------------------------------------


def test_every_supported_format_has_a_spec():
    for fmt in transcode.SUPPORTED_FORMATS:
        spec = transcode.format_spec(fmt)
        assert spec.extension.startswith(".")
        # Either a fixed encoder or a per-depth PCM table, never both/neither
        assert bool(spec.encoder) != bool(spec.pcm_codecs)


def test_lossless_and_lossy_partition_the_supported_formats():
    assert set(transcode.LOSSLESS_FORMATS) | set(transcode.LOSSY_FORMATS) == set(
        transcode.SUPPORTED_FORMATS
    )
    assert not set(transcode.LOSSLESS_FORMATS) & set(transcode.LOSSY_FORMATS)
    assert transcode.LOSSY_FORMATS == ("mp3",)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("flac", "flac"),
        (".FLAC", "flac"),
        ("  Flac  ", "flac"),
        ("alac", "alac"),
        ("m4a", "alac"),
        (".m4a", "alac"),
        ("apple-lossless", "alac"),
        ("wv", "wavpack"),
        ("wavpack", "wavpack"),
        ("wave", "wav"),
        ("aif", "aiff"),
        ("tta", "tta"),
        ("mp3_320", "mp3"),
        ("mp3-192", "mp3"),
        ("none", None),
        ("original", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_transcode_format(value, expected):
    assert transcode.normalize_transcode_format(value) == expected


def test_normalize_transcode_format_rejects_lossy_and_unknown_targets():
    # Deliberately unsupported: adding AAC/Opus/Vorbis output is a separate
    # decision, and silently accepting the name would produce nothing.
    for bogus in ("aac", "opus", "vorbis", "dsd", "ape", "tak", "banana"):
        with pytest.raises(ValueError, match="Unsupported transcode format"):
            transcode.normalize_transcode_format(bogus)


def test_alias_lookup_wins_over_the_bitrate_split():
    # "apple-lossless" must not be truncated to "apple" by the mp3_320 rule
    assert transcode.normalize_transcode_format("apple-lossless") == "alac"


# ---------------------------------------------------------------------------
# Extensions: the format name is not always the suffix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fmt", "extension"),
    [
        ("mp3", ".mp3"),
        ("flac", ".flac"),
        ("alac", ".m4a"),
        ("wav", ".wav"),
        ("aiff", ".aiff"),
        ("wavpack", ".wv"),
        ("tta", ".tta"),
    ],
)
def test_extension_for(fmt, extension):
    assert transcode.extension_for(fmt) == extension


def test_extension_for_disabled_transcoding():
    assert transcode.extension_for(None) == ""
    assert transcode.extension_for("") == ""


def test_transcoded_path_uses_the_container_extension(tmp_path):
    source = tmp_path / "song.flac"
    assert transcode.transcoded_path(source, "alac") == tmp_path / "song.m4a"
    assert transcode.transcoded_path(source, "wavpack") == tmp_path / "song.wv"
    assert transcode.transcoded_path(source, "mp3") == tmp_path / "song.mp3"


def test_format_spec_rejects_unknown_names():
    with pytest.raises(SpotiflacError):
        transcode.format_spec("aac")


def test_is_lossless():
    assert transcode.is_lossless("flac") is True
    assert transcode.is_lossless("alac") is True
    assert transcode.is_lossless("mp3") is False
    assert transcode.is_lossless(None) is False


# ---------------------------------------------------------------------------
# Bit depth → PCM encoder
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("depth", "expected"),
    [(8, 16), (16, 16), (20, 24), (24, 24), (32, 32), (64, 32)],
)
def test_nearest_pcm_depth_rounds_up(depth, expected):
    # Rounding up keeps the conversion lossless; rounding down would drop bits
    assert transcode._nearest_pcm_depth(depth) == expected


def test_pcm_encoder_follows_depth_and_container_endianness():
    wav = transcode.format_spec("wav")
    aiff = transcode.format_spec("aiff")
    assert wav.encoder_for(16) == "pcm_s16le"
    assert wav.encoder_for(24) == "pcm_s24le"
    assert aiff.encoder_for(16) == "pcm_s16be"
    assert aiff.encoder_for(24) == "pcm_s24be"


def test_non_pcm_formats_ignore_bit_depth():
    flac = transcode.format_spec("flac")
    assert flac.encoder_for(16) == flac.encoder_for(24) == "flac"


# ---------------------------------------------------------------------------
# ffmpeg command
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", transcode.LOSSLESS_FORMATS)
def test_lossless_commands_never_resample_or_requantise(fmt, tmp_path):
    spec = transcode.format_spec(fmt)
    cmd = transcode._encode_command(
        spec, tmp_path / "in.flac", tmp_path / "out", "320k", 24
    )
    # These are the flags that would silently break bit-exactness
    assert "-ar" not in cmd
    assert "-sample_fmt" not in cmd
    assert "-b:a" not in cmd
    assert "320k" not in cmd
    assert cmd[cmd.index("-f") + 1] == spec.muxer


def test_mp3_command_carries_the_bitrate(tmp_path):
    cmd = transcode._encode_command(
        transcode.format_spec("mp3"),
        tmp_path / "in.flac",
        tmp_path / "o.mp3",
        "192k",
        16,
    )
    assert cmd[cmd.index("-c:a") + 1] == "libmp3lame"
    assert cmd[cmd.index("-b:a") + 1] == "192k"


def test_flac_command_sets_the_compression_level(tmp_path):
    cmd = transcode._encode_command(
        transcode.format_spec("flac"),
        tmp_path / "i.wav",
        tmp_path / "o.flac",
        "320k",
        16,
    )
    assert cmd[cmd.index("-compression_level") + 1] == str(
        transcode.DEFAULT_FLAC_COMPRESSION
    )


# ---------------------------------------------------------------------------
# Real conversions
# ---------------------------------------------------------------------------


def _pcm_digest(path: Path) -> str:
    """md5 of the decoded samples — the only thing "lossless" has to preserve."""
    raw = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-f",
            "s32le",
            "-c:a",
            "pcm_s32le",
            "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    return hashlib.md5(raw).hexdigest()


#: A real (if tiny) JPEG. The cover has to survive a decode, because ffmpeg
#: parses the attached picture stream of the files these tests hand it.
_JPEG_1PX = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAA"
    "AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="
)

_FIXTURE_TAGS = {
    "TITLE": "Tone",
    "ARTIST": "Test",
    "ALBUM": "Fixtures",
    "TRACKNUMBER": "3",
}


@pytest.fixture
def tone(tmp_path):
    """A short tagged FLAC to convert, generated so the suite needs no binary fixture."""
    path = tmp_path / "tone.flac"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100:duration=1",
            "-c:a",
            "flac",
            str(path),
        ],
        check=True,
    )
    asyncio.run(
        tagger._write_tags_async(
            path, dict(_FIXTURE_TAGS), _JPEG_1PX, None, "", False, ".flac"
        )
    )
    return path


@needs_ffmpeg
@pytest.mark.parametrize("fmt", transcode.LOSSLESS_FORMATS)
def test_lossless_conversion_is_bit_exact_and_keeps_tags(fmt, tone):
    before = _pcm_digest(tone)

    dest = asyncio.run(
        transcode.transcode_file_async(tone, fmt=fmt, keep_original=False)
    )

    assert dest.suffix == transcode.extension_for(fmt)
    assert dest.exists()
    assert _pcm_digest(dest) == before, f"{fmt} altered the samples"

    embedded = tagger.read_embedded_tags(dest)
    assert embedded.tags.get("TITLE") == "Tone"
    assert embedded.tags.get("ARTIST") == "Test"
    assert embedded.tags.get("TRACKNUMBER") == "3"
    assert embedded.cover_data, f"{fmt} lost the cover art"


@needs_ffmpeg
def test_source_is_removed_unless_kept(tone, tmp_path):
    kept = tmp_path / "kept.flac"
    shutil.copy2(tone, kept)

    asyncio.run(transcode.transcode_file_async(kept, fmt="alac", keep_original=True))
    assert kept.exists()

    asyncio.run(transcode.transcode_file_async(tone, fmt="alac", keep_original=False))
    assert not tone.exists()


@needs_ffmpeg
def test_a_file_already_in_the_target_format_is_returned_untouched(tone):
    dest = asyncio.run(transcode.transcode_file_async(tone, fmt="flac"))
    assert dest == tone
    assert tone.exists()


@needs_ffmpeg
def test_alac_target_does_not_mistake_a_lossy_m4a_for_itself(tone, tmp_path):
    """An AAC .m4a has the right extension but the wrong codec.

    Returning it as "already ALAC" would label a lossy file lossless, so the
    check looks at the codec, not just the suffix.
    """
    aac = tmp_path / "lossy.m4a"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(tone),
            "-map",
            "0:a:0",
            "-vn",
            "-c:a",
            "aac",
            str(aac),
        ],
        check=True,
    )
    spec = transcode.format_spec("alac")
    assert transcode._already_in_target_format(aac, spec) is False

    alac = asyncio.run(transcode.transcode_file_async(tone, fmt="alac"))
    assert transcode._already_in_target_format(alac, spec) is True


def test_missing_source_raises(tmp_path):
    with pytest.raises(SpotiflacError, match="file not found"):
        asyncio.run(transcode.transcode_file_async(tmp_path / "ghost.flac", fmt="alac"))


# ---------------------------------------------------------------------------
# Probing the source
# ---------------------------------------------------------------------------


@needs_ffmpeg
def test_probe_reads_bit_depth_from_containers_mutagen_understands(tmp_path):
    src = tmp_path / "hires.flac"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=96000:duration=1",
            "-c:a",
            "flac",
            "-sample_fmt",
            "s32",
            "-bits_per_raw_sample",
            "24",
            str(src),
        ],
        check=True,
    )
    assert transcode._probe_source(src) == (24, True)


@needs_ffmpeg
def test_probe_falls_back_to_ffprobe_when_mutagen_has_no_bit_depth(tmp_path):
    """Regression: mutagen's TrueAudioInfo exposes no `bits_per_sample`.

    Trusting it alone defaulted every TTA source to 16-bit, which silently
    truncated a hi-res one on its way into a WAV or AIFF.
    """
    src = tmp_path / "hires.tta"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=96000:duration=1",
            "-c:a",
            "tta",
            "-sample_fmt",
            "s32",
            "-f",
            "tta",
            str(src),
        ],
        check=True,
    )
    # mutagen alone cannot answer for this container
    assert transcode._probe_with_mutagen(src) == (0, None)
    depth, lossless = transcode._probe_source(src)
    assert depth == 24
    assert lossless is True


@needs_ffmpeg
def test_probe_reports_a_lossy_source_as_such(tone, tmp_path):
    mp3 = tmp_path / "lossy.mp3"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(tone),
            "-map",
            "0:a:0",
            "-vn",
            "-c:a",
            "libmp3lame",
            str(mp3),
        ],
        check=True,
    )
    depth, lossless = transcode._probe_source(mp3)
    assert lossless is False
    assert depth == 16


def test_probe_assumes_hi_res_for_an_unreadable_lossless_container(tmp_path):
    """Neither tool can read a truncated file — guess up, never down.

    Assuming 16-bit here would truncate a real hi-res source on its way to
    WAV/AIFF; assuming 24 only costs bytes.
    """
    broken = tmp_path / "broken.wv"
    broken.write_bytes(b"not really wavpack")
    assert transcode._probe_source(broken) == (24, True)


@needs_ffmpeg
def test_hi_res_source_keeps_its_depth_through_a_pcm_target(tmp_path):
    src = tmp_path / "hires.flac"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=96000:duration=1",
            "-c:a",
            "flac",
            "-sample_fmt",
            "s32",
            "-bits_per_raw_sample",
            "24",
            str(src),
        ],
        check=True,
    )
    before = _pcm_digest(src)

    dest = asyncio.run(transcode.transcode_file_async(src, fmt="wav"))

    assert _pcm_digest(dest) == before
    declared = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=sample_rate,bits_per_raw_sample",
            "-of",
            "default=nw=1:nk=1",
            str(dest),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert declared == ["96000", "24"], declared


# ---------------------------------------------------------------------------
# TTA is an ID3 container, not an APEv2 one
# ---------------------------------------------------------------------------


@needs_ffmpeg
def test_tta_gets_id3_tags_rather_than_failing_on_apev2(tone):
    """Regression: .tta used to be routed to the APEv2 writer.

    mutagen models TrueAudio as an ID3FileType, so the APEv2 path raised
    "'3' not a Frame instance" and left every converted file untagged.
    """
    dest = asyncio.run(transcode.transcode_file_async(tone, fmt="tta"))

    embedded = tagger.read_embedded_tags(dest)
    assert embedded.tags.get("TITLE") == "Tone"
    assert embedded.tags.get("TRACKNUMBER") == "3"


# ---------------------------------------------------------------------------
# The three UIs must offer exactly what the core can produce
# ---------------------------------------------------------------------------

_FRONTEND = Path(__file__).resolve().parent.parent / "SpotiFLAC" / "frontend"


def test_web_ui_offers_every_supported_format():
    import re

    html = (_FRONTEND / "index.html").read_text(encoding="utf-8")
    select = re.search(r'<select id="config-transcode".*?</select>', html, re.S).group()
    values = set(re.findall(r'value="([^"]+)"', select))
    assert values == {"none", *transcode.SUPPORTED_FORMATS}


def test_web_ui_lossless_list_matches_the_core():
    """app.js hides the bitrate row from a hard-coded list; keep it honest."""
    import re

    js = (_FRONTEND / "app.js").read_text(encoding="utf-8")
    listed = re.search(r"LOSSLESS_TRANSCODE_FORMATS = \[(.*?)\]", js).group(1)
    assert set(re.findall(r"'([^']+)'", listed)) == set(transcode.LOSSLESS_FORMATS)


def test_every_frontend_offers_every_supported_format():
    """One menu, read by the CLI help, the wizard and the TUI alike."""
    offered = {value for _, value in transcode.TRANSCODE_CHOICES}
    assert offered == {None, *transcode.SUPPORTED_FORMATS}

    # The TUI builds its Select straight from it, and "" stands in for None
    # because a Select cannot hold None as an ordinary value.
    from SpotiFLAC.tui.config_view import ConfigPanel  # noqa: F401

    assert transcode.transcode_label(None) == transcode.TRANSCODE_CHOICES[0][0]
    assert transcode.transcode_label("mp3").startswith("MP3")


# ---------------------------------------------------------------------------
# DownloadResult.format must admit every container we can produce
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", transcode.SUPPORTED_FORMATS)
def test_every_target_produces_a_valid_download_result(fmt):
    """Regression: the pipeline reported the *format name* as the container.

    `DownloadResult.format` is a closed Literal of container names, so
    handing it "alac" (whose container is "m4a") failed validation and turned
    a finished download into "Unexpected error: 1 validation error".
    """
    from SpotiFLAC.core.models import DownloadResult

    label = transcode.result_format_for(fmt)
    result = DownloadResult.ok(
        "qobuz", f"/music/song{transcode.extension_for(fmt)}", label
    )
    assert result.format == label

    skipped = DownloadResult.skipped_result("qobuz", "/music/song", fmt=label)
    assert skipped.format == label


def test_result_format_is_the_extension_not_the_format_name():
    assert transcode.result_format_for("alac") == "m4a"
    assert transcode.result_format_for("wavpack") == "wv"
    assert transcode.result_format_for("flac") == "flac"
    assert transcode.result_format_for(None) is None


def test_audio_format_literal_covers_every_supported_target():
    """Adding a format to core.transcode must widen models.AudioFormat too."""
    from typing import get_args

    from SpotiFLAC.core.models import AudioFormat

    produced = {transcode.result_format_for(f) for f in transcode.SUPPORTED_FORMATS}
    assert produced <= set(get_args(AudioFormat))


# ---------------------------------------------------------------------------
# The wizard's "Equivalent CLI command" has to actually be equivalent
# ---------------------------------------------------------------------------

_WIZARD_BASE = {
    "url": "https://open.spotify.com/album/x",
    "output_dir": "/music",
    "services": ["qobuz"],
    "quality": "LOSSLESS",
    "filename_format": "{title} - {artist}",
    "use_track_numbers": False,
    "use_album_track_numbers": False,
    "use_artist_subfolders": False,
    "use_album_subfolders": False,
    "first_artist_only": False,
    "embed_lyrics": True,
    "lyrics_providers": ["apple"],
    "enrich_metadata": True,
    "enrich_providers": ["deezer"],
}


def _printed_command(cfg: dict) -> list[str]:
    """The argv a configuration corresponds to.

    Straight from the builder in core/cli_preview.py rather than through a
    frontend's printing of it: what these tests are about is which flags a
    configuration produces, not how any one UI renders them.
    """
    from SpotiFLAC.core.cli_preview import build_command_parts

    return build_command_parts(cfg)


def _reparse(argv: list[str]):
    import sys

    from SpotiFLAC.downloader import DownloadOptions
    from SpotiFLAC.launcher import parse_args

    original = sys.argv
    try:
        sys.argv = argv
        args = parse_args({})
    finally:
        sys.argv = original
    return DownloadOptions(
        output_dir=args.output_dir,
        transcode_to=args.transcode_to,
        transcode_bitrate=args.transcode_bitrate,
        transcode_keep_original=args.transcode_keep_original,
    )


@pytest.mark.parametrize("fmt", transcode.SUPPORTED_FORMATS)
def test_printed_command_round_trips_every_transcode_target(fmt):
    """Regression: the builder emitted no transcode flag at all.

    Copying the printed command and running it silently produced unconverted
    files, which is the one thing an "equivalent" command must not do.
    """
    opts = _reparse(_printed_command({**_WIZARD_BASE, "transcode_to": fmt}))
    assert opts.transcode_to == fmt


def test_printed_command_round_trips_keep_original_and_bitrate():
    cfg = {
        **_WIZARD_BASE,
        "transcode_to": "mp3",
        "transcode_bitrate": "192k",
        "transcode_keep_original": True,
    }
    opts = _reparse(_printed_command(cfg))
    assert (opts.transcode_to, opts.transcode_bitrate) == ("mp3", "192k")
    assert opts.transcode_keep_original is True


def test_printed_command_omits_the_bitrate_for_lossless_targets():
    """--transcode-bitrate beside --transcode flac would advertise a no-op."""
    argv = _printed_command(
        {**_WIZARD_BASE, "transcode_to": "flac", "transcode_bitrate": "192k"}
    )
    assert "--transcode-bitrate" not in argv


def test_printed_command_omits_transcode_flags_when_disabled():
    argv = _printed_command(dict(_WIZARD_BASE))
    assert "--transcode" not in argv
    assert "--keep-original" not in argv


@pytest.mark.parametrize(
    ("key", "value", "dest"),
    [
        ("m3u_format", "m3u", "m3u_format"),
        ("verify_hires", True, "verify_hires"),
        ("resume", False, "resume"),
        ("include_featuring", True, "include_featuring"),
        ("post_download_hooks", ["mylib.hooks:on_track"], "post_hooks"),
    ],
)
def test_printed_command_round_trips_the_remaining_profile_settings(key, value, dest):
    """A loaded profile feeds cfg straight into the builder (interactive.py:868).

    Every one of these was silently dropped from the printed command, so a
    profile with them set produced a command that quietly did something else.
    """
    import sys

    from SpotiFLAC.launcher import parse_args

    argv = _printed_command({**_WIZARD_BASE, key: value})
    original = sys.argv
    try:
        sys.argv = argv
        args = parse_args({})
    finally:
        sys.argv = original
    assert getattr(args, dest) == value


def test_printed_command_stays_clean_at_the_defaults():
    argv = _printed_command(dict(_WIZARD_BASE))
    for flag in (
        "--m3u",
        "--verify-hires",
        "--no-resume",
        "--include-featuring",
        "--post-hook",
    ):
        assert flag not in argv


def test_profile_model_keeps_every_key_save_profile_writes():
    """Regression: ProfileConfig sets extra="ignore".

    `--verify-hires --save-profile x` wrote the key and the model dropped it
    on the way in, so the setting vanished with no error anywhere.
    """
    import re
    from pathlib import Path

    from SpotiFLAC.core.profiles import ProfileConfig

    launcher = Path(__file__).resolve().parent.parent / "SpotiFLAC" / "launcher.py"
    src = launcher.read_text(encoding="utf-8")
    block = src[src.index("profile_cfg = {") :]
    block = block[: block.index("}\n")]
    written = set(re.findall(r'"([a-z_]+)":', block))

    assert written <= set(ProfileConfig.model_fields)


# ---------------------------------------------------------------------------
# --fallback / --no-fallback
# ---------------------------------------------------------------------------


def _parse(argv: list[str], profile_defaults: dict | None = None):
    import sys

    from SpotiFLAC.launcher import parse_args

    original = sys.argv
    try:
        sys.argv = ["spotiflac", "https://x/y", "/out", *argv]
        return parse_args(profile_defaults or {})
    finally:
        sys.argv = original


def test_fallback_defaults_to_enabled():
    assert _parse([]).allow_fallback is True


def test_no_fallback_turns_it_off():
    assert _parse(["--no-fallback"]).allow_fallback is False


def test_fallback_can_override_a_profile_that_disabled_it():
    """Why the flag is a pair and not a lone --no-fallback.

    A profile can carry allow_fallback=False; without --fallback there is no
    way to re-enable it for a single run.
    """
    defaults = {"allow_fallback": False}
    assert _parse([], defaults).allow_fallback is False
    assert _parse(["--fallback"], defaults).allow_fallback is True


def test_printed_command_round_trips_the_fallback_choice():
    """Regression: the CLI hardcoded allow_fallback=True.

    The wizard asks the question, the profile stores the answer, and both
    were discarded the moment the run went through the command line.
    """
    for value in (True, False):
        argv = _printed_command({**_WIZARD_BASE, "allow_fallback": value})
        assert _parse(argv[3:]).allow_fallback is value

    assert "--no-fallback" not in _printed_command(dict(_WIZARD_BASE))


# ---------------------------------------------------------------------------
# --log-level
# ---------------------------------------------------------------------------


def _level(argv: list[str], profile_defaults: dict | None = None) -> int:
    from SpotiFLAC.launcher import _resolve_log_level

    args = _parse(argv, profile_defaults)
    return _resolve_log_level(args.verbose, args.log_level, args.profile_log_level)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("DEBUG", logging.DEBUG),
        ("info", logging.INFO),
        ("warn", logging.WARNING),
        ("CRIT", logging.CRITICAL),
        ("10", logging.DEBUG),
    ],
)
def test_log_level_accepts_names_aliases_and_numbers(given, expected):
    assert _level(["--log-level", given]) == expected


def test_log_level_rejects_an_unknown_name():
    with pytest.raises(SystemExit):
        _parse(["--log-level", "LOUD"])


def test_log_level_beats_verbose():
    assert _level(["-v"]) == logging.DEBUG
    assert _level(["-v", "--log-level", "CRITICAL"]) == logging.CRITICAL


def test_verbose_beats_a_level_stored_in_a_profile():
    """A saved preference must not outrank a flag typed for this run."""
    defaults = {"log_level": logging.INFO}
    assert _level([], defaults) == logging.INFO
    assert _level(["-v"], defaults) == logging.DEBUG
    assert _level(["--log-level", "CRITICAL"], defaults) == logging.CRITICAL


@pytest.mark.parametrize(
    "level",
    [logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL],
)
def test_printed_command_round_trips_the_log_level(level):
    """Profiles store the number; the printed command has to say the name."""
    argv = _printed_command({**_WIZARD_BASE, "log_level": level})
    assert logging.getLevelName(level) in argv
    assert _level(argv[3:]) == level


def test_printed_command_omits_the_log_level_when_unset():
    assert "--log-level" not in _printed_command(dict(_WIZARD_BASE))


# ---------------------------------------------------------------------------
# Output paths handed to extensions must be absolute
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("output_dir", ["Downloads", "./out", "out/nested"])
def test_output_path_is_absolute_for_a_relative_output_dir(
    output_dir, tmp_path, monkeypatch
):
    """Regression: a relative output_dir made host and extension disagree.

    The path is handed to JS extensions as a plain string, and their Node
    process runs with its own cwd (the extension folder). With a relative
    path the extension wrote under ~/.spotiflac/extensions/<name>/, while
    the host looked in ./<output_dir>/ — the download then failed as
    "Download returned no usable audio (segments unmergeable)".
    """
    from SpotiFLAC.core.models import TrackMetadata
    from SpotiFLAC.extensions.provider import JSExtensionProvider

    monkeypatch.chdir(tmp_path)
    provider = JSExtensionProvider.__new__(JSExtensionProvider)
    provider.name = "ext:tidal-web"

    path = provider._build_output_path(
        metadata=TrackMetadata(
            id="x",
            title="Tone",
            artists="Test",
            album="Fixtures",
            album_artist="Test",
        ),
        output_dir=output_dir,
        filename_format="{title} - {artist}",
        position=1,
        include_track_num=False,
        use_album_track_num=False,
        first_artist_only=False,
        extension=".flac",
        native_id="1",
    )

    assert path.is_absolute(), f"{output_dir!r} produced a relative path"
    assert path == path.resolve()
    assert path.parent.is_dir()


# ---------------------------------------------------------------------------
# The default level shows what the run is doing
# ---------------------------------------------------------------------------


def test_default_level_is_info_so_download_milestones_are_visible():
    """The ticket, the audio fetch and the transcode are all logger.info.

    At the old ERROR default a run that hung printed nothing at all until it
    gave up, which is why every diagnosis needed --verbose and its flood.
    """
    assert _level([]) == logging.INFO


def test_noisy_libraries_are_pinned_below_the_default_level():
    """INFO is only usable as a default if httpx isn't logging every request."""
    from SpotiFLAC.launcher import _NOISY_LIBRARY_LOGGERS, _quiet_noisy_libraries

    _quiet_noisy_libraries(logging.INFO)
    for name in _NOISY_LIBRARY_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING


def test_debug_lifts_the_pin_on_libraries():
    """A network problem is exactly when httpcore's frames are worth reading."""
    from SpotiFLAC.launcher import _NOISY_LIBRARY_LOGGERS, _quiet_noisy_libraries

    _quiet_noisy_libraries(logging.DEBUG)
    for name in _NOISY_LIBRARY_LOGGERS:
        assert logging.getLogger(name).level == logging.NOTSET


def test_a_quieter_level_can_still_be_asked_for():
    assert _level(["--log-level", "ERROR"]) == logging.ERROR
    assert _level(["--log-level", "WARNING"]) == logging.WARNING


def test_signed_session_names_the_audio_fetch_at_info():
    """Regression: POST /dl was logged at debug.

    The ticket was named at info but the request that actually fetches the
    audio was not, so at the default level a download that stalled on the
    fetch looked identical to one that never started it.
    """
    from pathlib import Path

    src = (
        Path(__file__).resolve().parent.parent
        / "SpotiFLAC"
        / "core"
        / "signed_session_mobile.py"
    ).read_text(encoding="utf-8")
    block = src[src.index('is_ticket = "/tickets" in path') :][:1200]
    assert 'is_download = "/dl" in path' in block
    assert "Fetching audio" in block
    # and it must be info, not the debug branch it used to fall through to
    fetch_at = block.index("Fetching audio")
    assert "logger.info(" in block[fetch_at - 120 : fetch_at]
