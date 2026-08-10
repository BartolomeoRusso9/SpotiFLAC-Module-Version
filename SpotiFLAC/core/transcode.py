"""Post-download transcoding.

Providers always fetch the best source available (FLAC, ALAC/M4A, …). When
`DownloadOptions.transcode_to` is set the orchestrator converts every finished
track to that format — currently MP3 (320 kbps by default) via ffmpeg/libmp3lame.

The converted file keeps the exact same path as the source, only with a
different extension, so the "already downloaded" check keeps working: the
downloader looks for the *transcoded* filename before contacting any provider
and skips the track when it is already there.
"""

from __future__ import annotations

import asyncio
import asyncio.subprocess as _subproc
import contextlib
import logging
import os
from pathlib import Path

from .errors import ErrorKind, SpotiflacError
from .tagger import transfer_tags_to_mp3_async

logger = logging.getLogger(__name__)

MP3 = "mp3"
SUPPORTED_FORMATS: tuple[str, ...] = (MP3,)
DEFAULT_MP3_BITRATE = "320k"

# Valori che disabilitano la conversione (utili quando arrivano da GUI/config)
_DISABLED_VALUES = {"", "none", "off", "no", "original", "source", "keep"}


def normalize_transcode_format(value: str | None) -> str | None:
    """Normalizes the requested target format.

    Accepts ``"mp3"``, ``".mp3"``, ``"MP3"`` and the 320 kbps aliases used by
    the GUI (``"mp3_320"``). Returns None when transcoding is disabled and
    raises ValueError for formats we cannot produce.
    """
    if value is None:
        return None

    normalized = str(value).strip().lower().lstrip(".")
    if normalized in _DISABLED_VALUES:
        return None

    # "mp3_320" / "mp3-320" → "mp3"
    base = normalized.replace("-", "_").split("_")[0]
    if base in SUPPORTED_FORMATS:
        return base

    msg = (
        f"Unsupported transcode format: {value!r}. "
        f"Supported: {', '.join(SUPPORTED_FORMATS)}"
    )
    raise ValueError(msg)


def normalize_bitrate(value: str | int | None) -> str:
    """Normalizes a bitrate into the ffmpeg form (``"320k"``)."""
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
    return Path(source).with_suffix(f".{fmt}")


def transcoded_file_exists(path: Path | str) -> bool:
    """True when a previously transcoded file is already on disk and non-empty."""
    p = Path(path)
    try:
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def ensure_ffmpeg_available(fmt: str) -> None:
    """Raises SpotiflacError when ffmpeg — required for transcoding — is missing."""
    from .ffmpeg_check import check_ffmpeg

    result = check_ffmpeg()
    if result.get("available"):
        return

    msg = (
        f"Transcoding to {fmt.upper()} requires ffmpeg, which is not available "
        f"({result.get('error') or 'unknown error'}). "
        "Install it from https://ffmpeg.org/download.html or disable transcoding."
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


def _mp3_command(src: Path, dest: Path, bitrate: str) -> list[str]:
    return [
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
        "libmp3lame",
        "-b:a",
        bitrate,
        "-id3v2_version",
        "3",
        "-write_id3v1",
        "1",
        "-f",
        "mp3",
        str(dest),
    ]


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

    Raises
    ------
        SpotiflacError: If the source is missing or ffmpeg fails.

    """
    src = Path(source)
    if fmt not in SUPPORTED_FORMATS:
        msg = f"Unsupported transcode format: {fmt}"
        raise SpotiflacError(ErrorKind.FILE_IO, msg)

    dest = transcoded_path(src, fmt)
    if src.suffix.lower() == dest.suffix.lower():
        # Il provider ha già prodotto il formato richiesto
        return src

    if not await asyncio.to_thread(src.is_file):
        msg = f"Cannot transcode, file not found: {src}"
        raise SpotiflacError(ErrorKind.FILE_IO, msg)

    tmp = dest.with_name(f".{dest.stem}.spotiflac-transcode.{fmt}")
    rc, stderr = await _run_ffmpeg(*_mp3_command(src, tmp, bitrate))

    if rc != 0 or not transcoded_file_exists(tmp):
        with contextlib.suppress(OSError):
            await asyncio.to_thread(tmp.unlink, True)
        msg = f"ffmpeg failed to transcode {src.name} to {fmt.upper()}: {stderr}"
        raise SpotiflacError(ErrorKind.FILE_IO, msg)

    try:
        await transfer_tags_to_mp3_async(src, tmp)
    except Exception as exc:
        # ffmpeg already carried over the basic tags via -map_metadata:
        # a partial tag set is not worth discarding the conversion.
        logger.warning("[transcode] tag transfer failed for %s: %s", src.name, exc)

    await asyncio.to_thread(os.replace, tmp, dest)

    if not keep_original:
        try:
            await asyncio.to_thread(src.unlink)
        except OSError as exc:
            logger.warning("[transcode] could not remove source %s: %s", src.name, exc)

    logger.info(
        "[transcode] %s → %s (%s)",
        src.name,
        dest.name,
        bitrate,
    )
    return dest
