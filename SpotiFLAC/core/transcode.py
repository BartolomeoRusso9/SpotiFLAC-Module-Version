"""Post-download transcoding.

Providers always fetch the best source available (FLAC, ALAC/M4A, …). When
`DownloadOptions.transcode_to` is set the orchestrator converts every finished
track to that format via ffmpeg.

Two families of targets are supported:

* **Lossless** — FLAC, ALAC (`.m4a`), WAV, AIFF, WavPack (`.wv`) and
  TTA (`.tta`). The conversion is bit-exact: the decoded samples are
  re-encoded without resampling and without changing the bit depth, so
  the result decodes back to the very same PCM as the source.
* **Lossy** — MP3 (320 kbps by default), the original reason this module
  exists.

The converted file keeps the exact same path as the source, only with a
different extension, so the "already downloaded" check keeps working: the
downloader looks for the *transcoded* filename before contacting any provider
and skips the track when it is already there. Note that the extension is not
always the format name — ALAC lives inside an `.m4a` container and WavPack
inside `.wv` — so always go through `extension_for()` instead of formatting
`f".{fmt}"` by hand.
"""

from __future__ import annotations

import asyncio
import asyncio.subprocess as _subproc
import contextlib
import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .errors import ErrorKind, SpotiflacError
from .tagger import transfer_tags_async

logger = logging.getLogger(__name__)

MP3 = "mp3"
FLAC = "flac"
ALAC = "alac"
WAV = "wav"
AIFF = "aiff"
WAVPACK = "wavpack"
TTA = "tta"

DEFAULT_MP3_BITRATE = "320k"

#: ffmpeg's FLAC compression level. 8 is the strongest setting the encoder
#: offers; it buys ~1-2% over the default 5 for roughly twice the CPU time.
#: A download is converted once and kept forever, so the trade favours size.
DEFAULT_FLAC_COMPRESSION = 8

# PCM has no single "codec": the container picks a fixed-width, fixed-endian
# encoder per bit depth. WAV is little-endian, AIFF big-endian.
_PCM_LITTLE_ENDIAN: Mapping[int, str] = {
    16: "pcm_s16le",
    24: "pcm_s24le",
    32: "pcm_s32le",
}
_PCM_BIG_ENDIAN: Mapping[int, str] = {
    16: "pcm_s16be",
    24: "pcm_s24be",
    32: "pcm_s32be",
}


@dataclass(frozen=True)
class FormatSpec:
    """Everything needed to encode one target format with ffmpeg."""

    name: str
    extension: str
    lossless: bool
    muxer: str
    #: Fixed encoder name. Empty for PCM, where `pcm_codecs` decides.
    encoder: str = ""
    #: Bit depth → encoder, for the PCM containers (WAV/AIFF).
    pcm_codecs: Mapping[int, str] | None = None
    #: Spellings accepted by `normalize_transcode_format()` besides `name`.
    aliases: tuple[str, ...] = field(default_factory=tuple)

    def encoder_for(self, bit_depth: int) -> str:
        """The ffmpeg encoder to use for a source of the given bit depth."""
        if self.pcm_codecs is None:
            return self.encoder
        return self.pcm_codecs[_nearest_pcm_depth(bit_depth)]


_FORMAT_SPECS: tuple[FormatSpec, ...] = (
    FormatSpec(
        name=MP3,
        extension=".mp3",
        lossless=False,
        muxer="mp3",
        encoder="libmp3lame",
    ),
    FormatSpec(
        name=FLAC,
        extension=".flac",
        lossless=True,
        muxer="flac",
        encoder="flac",
    ),
    FormatSpec(
        # ALAC is a codec, not a container: it ships inside MPEG-4, which is
        # why the extension is .m4a and the muxer is ffmpeg's "ipod".
        name=ALAC,
        extension=".m4a",
        lossless=True,
        muxer="ipod",
        encoder="alac",
        aliases=("m4a", "applelossless", "apple lossless", "apple-lossless"),
    ),
    FormatSpec(
        name=WAV,
        extension=".wav",
        lossless=True,
        muxer="wav",
        pcm_codecs=_PCM_LITTLE_ENDIAN,
        aliases=("wave", "pcm", "riff"),
    ),
    FormatSpec(
        name=AIFF,
        extension=".aiff",
        lossless=True,
        muxer="aiff",
        pcm_codecs=_PCM_BIG_ENDIAN,
        aliases=("aif", "aifc"),
    ),
    FormatSpec(
        name=WAVPACK,
        extension=".wv",
        lossless=True,
        muxer="wv",
        encoder="wavpack",
        aliases=("wv",),
    ),
    FormatSpec(
        name=TTA,
        extension=".tta",
        lossless=True,
        muxer="tta",
        encoder="tta",
        aliases=("trueaudio", "true audio"),
    ),
)

_SPEC_BY_NAME: dict[str, FormatSpec] = {spec.name: spec for spec in _FORMAT_SPECS}
_SPEC_BY_ALIAS: dict[str, FormatSpec] = {
    alias: spec for spec in _FORMAT_SPECS for alias in (spec.name, *spec.aliases)
}

SUPPORTED_FORMATS: tuple[str, ...] = tuple(spec.name for spec in _FORMAT_SPECS)
LOSSLESS_FORMATS: tuple[str, ...] = tuple(
    spec.name for spec in _FORMAT_SPECS if spec.lossless
)
LOSSY_FORMATS: tuple[str, ...] = tuple(
    spec.name for spec in _FORMAT_SPECS if not spec.lossless
)

# Values that disable conversion (useful when they come from GUI/config)
_DISABLED_VALUES = {"", "none", "off", "no", "original", "source", "keep"}

#: MPEG-4 containers, where the extension alone does not say whether the
#: audio inside is lossless (ALAC) or lossy (AAC).
_EXT_MP4 = frozenset({".m4a", ".mp4", ".m4b"})

#: Containers that only ever hold lossless audio. Used as the last-resort
#: answer when neither mutagen nor ffprobe could read a bit depth.
_LOSSLESS_SUFFIXES = frozenset(
    {".flac", ".wav", ".wave", ".aiff", ".aif", ".wv", ".tta", ".ape", ".alac"}
)


def _nearest_pcm_depth(depth: int) -> int:
    """Rounds an arbitrary bit depth up to one PCM actually encodes.

    Rounding *up* matters: a 20-bit source stored as 24-bit PCM is still
    bit-exact, whereas truncating it to 16 would quietly throw samples away.
    """
    if depth <= 16:
        return 16
    if depth <= 24:
        return 24
    return 32


def format_spec(fmt: str) -> FormatSpec:
    """Returns the `FormatSpec` for an already-normalized format name."""
    try:
        return _SPEC_BY_NAME[fmt]
    except KeyError:
        msg = (
            f"Unsupported transcode format: {fmt!r}. "
            f"Supported: {', '.join(SUPPORTED_FORMATS)}"
        )
        raise SpotiflacError(ErrorKind.FILE_IO, msg) from None


def extension_for(fmt: str | None) -> str:
    """File extension a track gets once converted to `fmt` (``".m4a"`` for ALAC).

    Returns an empty string when transcoding is disabled, so callers can
    build a path unconditionally and check the result.
    """
    if not fmt:
        return ""
    return format_spec(fmt).extension


def result_format_for(fmt: str | None) -> str | None:
    """The `DownloadResult.format` label for a transcode target.

    That field names the *container*, not the codec — the codebase's own
    `_ext_to_fmt()` maps ".m4a" to "m4a" — so a target whose name differs
    from its extension has to be translated: "alac" is reported as "m4a"
    and "wavpack" as "wv", exactly as if a provider had delivered the file.
    """
    extension = extension_for(fmt)
    return extension.lstrip(".") or None


def is_lossless(fmt: str | None) -> bool:
    """True when `fmt` is one of the lossless targets."""
    return bool(fmt) and format_spec(str(fmt)).lossless


def normalize_transcode_format(value: str | None) -> str | None:
    """Normalizes the requested target format.

    Accepts the canonical names (``"flac"``, ``"alac"``, ``"mp3"``, …), the
    usual spellings for each (``".m4a"``, ``"WV"``, ``"aif"``, ``"wave"``)
    and the bitrate-suffixed aliases the GUI sends (``"mp3_320"``). Returns
    None when transcoding is disabled and raises ValueError for formats we
    cannot produce.
    """
    if value is None:
        return None

    normalized = str(value).strip().lower().lstrip(".")
    if normalized in _DISABLED_VALUES:
        return None

    spec = _SPEC_BY_ALIAS.get(normalized)
    if spec is not None:
        return spec.name

    # "mp3_320" / "mp3-320" → "mp3". Only the leading token is a format
    # name here, so this runs *after* the alias lookup: "apple-lossless"
    # must not be truncated to "apple".
    base = normalized.replace("-", "_").split("_")[0]
    spec = _SPEC_BY_ALIAS.get(base)
    if spec is not None:
        return spec.name

    msg = (
        f"Unsupported transcode format: {value!r}. "
        f"Supported: {', '.join(SUPPORTED_FORMATS)}"
    )
    raise ValueError(msg)


def normalize_bitrate(value: str | int | None) -> str:
    """Normalizes a bitrate into the ffmpeg form (``"320k"``).

    Only meaningful for the lossy targets; the lossless encoders ignore it.
    """
    if value is None:
        return DEFAULT_MP3_BITRATE

    normalized = str(value).strip().lower().replace("kbps", "k").replace(" ", "")
    if not normalized:
        return DEFAULT_MP3_BITRATE
    if normalized.isdigit():
        # 320 → "320k", 320000 → "320000" (ffmpeg accepts raw bit/s too)
        return f"{normalized}k" if int(normalized) <= 3000 else normalized
    return normalized


def transcoded_path(source: Path | str, fmt: str = MP3) -> Path:
    """Returns the path `source` will have once converted to `fmt`."""
    return Path(source).with_suffix(extension_for(fmt))


def transcoded_file_exists(path: Path | str) -> bool:
    """True when a previously transcoded file is already on disk and non-empty."""
    p = Path(path)
    try:
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def ensure_ffmpeg_available(fmt: str) -> None:
    """Raises SpotiflacError when ffmpeg — required for transcoding — is
    still missing after a best-effort automatic install attempt (see
    ffmpeg_check.ensure_ffmpeg_installed()).
    """
    from .ffmpeg_check import ensure_ffmpeg_installed

    result = ensure_ffmpeg_installed()
    if result.get("available"):
        return

    msg = (
        f"Transcoding to {fmt.upper()} requires ffmpeg, and automatic "
        f"installation didn't work: {result.get('error') or 'unknown error'}"
    )
    raise SpotiflacError(ErrorKind.FILE_IO, msg)


async def _run_ffmpeg(*args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        *args,
        stdout=_subproc.PIPE,
        stderr=_subproc.PIPE,
    )
    _, stderr = await proc.communicate()
    return proc.returncode, stderr.decode(errors="ignore").strip()


def _probe_with_mutagen(src: Path) -> tuple[int, bool | None]:
    """`(bit_depth, is_lossless)`, or `(0, None)` when mutagen cannot tell.

    Tried first because mutagen is already a hard dependency and costs no
    subprocess. It does not know every container though — `TrueAudioInfo`,
    for one, exposes no `bits_per_sample` at all — hence the None verdict
    for the caller to resolve.
    """
    try:
        from mutagen import File as MutagenFile

        info = getattr(MutagenFile(str(src)), "info", None)
        if info is None:
            return 0, None

        depth = int(getattr(info, "bits_per_sample", 0) or 0)
        if not depth:
            return 0, None

        if src.suffix.lower() in _EXT_MP4:
            # AAC and ALAC share the MPEG-4 container and both report a bit
            # depth, so only the codec tells a lossy download from a
            # lossless one.
            codec = str(getattr(info, "codec", "") or "").lower()
            return depth, codec.startswith("alac")
        return depth, True
    except Exception as exc:
        logger.debug("[transcode] mutagen could not probe %s: %s", src.name, exc)
        return 0, None


def _probe_depth_with_ffprobe(src: Path) -> int:
    """Bit depth per ffprobe, or 0 when it reports none.

    Only lossless codecs declare `bits_per_raw_sample`; MP3, AAC and friends
    leave it unset, which makes its absence a usable "this is lossy" signal.
    ffprobe ships with ffmpeg, already a hard requirement for transcoding.
    """
    try:
        out = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=bits_per_raw_sample",
                "-of",
                "default=nw=1:nk=1",
                str(src),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        ).stdout.strip()
        return int(out) if out.isdigit() else 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        logger.debug("[transcode] ffprobe could not probe %s: %s", src.name, exc)
        return 0


def _probe_source(src: Path) -> tuple[int, bool]:
    """Returns `(bit_depth, source_is_lossless)` for an audio file.

    The depth drives the PCM encoder choice for the WAV/AIFF targets, so
    every fallback here rounds *towards* keeping data: guessing too high
    only wastes bytes, guessing too low silently truncates samples.
    """
    depth, verdict = _probe_with_mutagen(src)
    if verdict is not None:
        return depth, verdict

    depth = _probe_depth_with_ffprobe(src)
    if depth:
        return depth, True

    # Neither tool found a bit depth. For a lossy codec that is the correct
    # answer; for a lossless container we could not inspect, assume 24-bit
    # rather than risk truncating a hi-res source.
    if src.suffix.lower() in _LOSSLESS_SUFFIXES:
        return 24, True
    return 16, False


def _already_in_target_format(src: Path, spec: FormatSpec) -> bool:
    """True when the provider already delivered exactly what was requested.

    The extension alone is not enough for ALAC: a lossy AAC download also
    lands in an `.m4a`, and returning it as "already ALAC" would mislabel
    it as lossless.
    """
    if src.suffix.lower() != spec.extension:
        return False
    if spec.name != ALAC:
        return True
    _, lossless = _probe_source(src)
    return lossless


def _encode_command(
    spec: FormatSpec,
    src: Path,
    dest: Path,
    bitrate: str,
    bit_depth: int,
) -> list[str]:
    cmd = [
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        # Only the first audio stream: embedded artwork is re-attached later
        # by the tagger, which handles every source container the same way.
        "-map",
        "0:a:0",
        "-vn",
        "-map_metadata",
        "0",
        "-c:a",
        spec.encoder_for(bit_depth),
    ]

    if spec.name == MP3:
        cmd += ["-b:a", bitrate, "-id3v2_version", "3", "-write_id3v1", "1"]
    elif spec.name == FLAC:
        cmd += ["-compression_level", str(DEFAULT_FLAC_COMPRESSION)]

    # No -ar / -sample_fmt anywhere: leaving the rate and depth untouched is
    # what makes the lossless targets actually lossless.
    cmd += ["-f", spec.muxer, str(dest)]
    return cmd


async def transcode_file_async(
    source: Path | str,
    *,
    fmt: str = MP3,
    bitrate: str = DEFAULT_MP3_BITRATE,
    keep_original: bool = False,
) -> Path:
    """Converts `source` to `fmt`, preserving tags, cover art and lyrics.

    The output lands next to the source with the same stem. The source is
    removed once the conversion succeeds unless `keep_original` is set.
    Returns the path of the converted file.

    For the lossless targets the sample rate and bit depth of the source are
    carried over untouched, so the conversion round-trips to identical PCM.

    Raises
    ------
        SpotiflacError: If the source is missing or ffmpeg fails.

    """
    src = Path(source)
    spec = format_spec(fmt)

    dest = transcoded_path(src, spec.name)
    if _already_in_target_format(src, spec):
        # The provider already produced the requested format
        return src

    if not await asyncio.to_thread(src.is_file):
        msg = f"Cannot transcode, file not found: {src}"
        raise SpotiflacError(ErrorKind.FILE_IO, msg)

    bit_depth, source_is_lossless = await asyncio.to_thread(_probe_source, src)
    if spec.lossless and not source_is_lossless:
        # Worth saying out loud: the output will be a bit-perfect copy of an
        # already-degraded signal, not a recovered original. Not an error —
        # some libraries are deliberately single-format.
        logger.warning(
            "[transcode] %s is lossy: converting it to %s cannot restore "
            "quality, only grow the file",
            src.name,
            spec.name.upper(),
        )

    tmp = dest.with_name(f".{dest.stem}.spotiflac-transcode{spec.extension}")
    rc, stderr = await _run_ffmpeg(*_encode_command(spec, src, tmp, bitrate, bit_depth))

    if rc != 0 or not transcoded_file_exists(tmp):
        with contextlib.suppress(OSError):
            await asyncio.to_thread(tmp.unlink, True)
        msg = f"ffmpeg failed to transcode {src.name} to {spec.name.upper()}: {stderr}"
        raise SpotiflacError(ErrorKind.FILE_IO, msg)

    try:
        await transfer_tags_async(src, tmp)
    except Exception as exc:
        # ffmpeg already carried over the basic tags via -map_metadata:
        # a partial tag set is not worth discarding the conversion.
        logger.warning("[transcode] tag transfer failed for %s: %s", src.name, exc)

    await asyncio.to_thread(os.replace, tmp, dest)

    if not keep_original and dest != src:
        try:
            await asyncio.to_thread(src.unlink)
        except OSError as exc:
            logger.warning("[transcode] could not remove source %s: %s", src.name, exc)

    logger.info(
        "[transcode] %s → %s (%s)",
        src.name,
        dest.name,
        bitrate if not spec.lossless else f"{bit_depth}-bit lossless",
    )
    return dest
