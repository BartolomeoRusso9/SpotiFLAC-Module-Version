"""Fetch Apple Music animated album artwork and write Navidrome-compatible WebP."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx

DEFAULT_ARTWORK_API_URL = "https://artwork.m8tec.top"
ARTWORK_API_URL_ENV = "MORNYE_ANIMATED_ARTWORKS_API_URL"
ARTWORK_ENABLED_ENV = "MORNYE_ANIMATED_ARTWORKS_ENABLED"
MAX_ARTWORK_BYTES = 32 * 1024 * 1024
_MAX_ARTWORK_SECONDS = 20


class AnimatedArtworkError(Exception):
    """The artwork service or its media response could not be used."""


def animated_artworks_enabled_by_default() -> bool:
    """Whether animated artwork should be enabled in new configurations."""
    return os.getenv(ARTWORK_ENABLED_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def artwork_api_url() -> str:
    """Return the configured API root, defaulting to the public instance."""
    configured = os.getenv(ARTWORK_API_URL_ENV, "").strip()
    return (configured or DEFAULT_ARTWORK_API_URL).rstrip("/")


def _valid_api_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 80, 443)
        )
    except ValueError:
        return False


def _valid_stream_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        return (
            parsed.scheme == "https"
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
            and (host == "apple.com" or host.endswith(".apple.com"))
        )
    except ValueError:
        return False


async def fetch_animated_artwork_url(
    artist: str,
    album: str,
    *,
    api_url: str | None = None,
) -> str | None:
    """Look up an album's HLS stream in the animated-artwork API."""
    base_url = (api_url or artwork_api_url()).strip().rstrip("/")
    if not _valid_api_url(base_url):
        raise AnimatedArtworkError("animated artwork API URL must be HTTP(S)")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
            response = await client.get(
                f"{base_url}/api/v1/artwork/search",
                params={"artist": artist, "album": album},
            )
    except httpx.HTTPError as exc:
        raise AnimatedArtworkError(f"artwork API request failed: {exc}") from exc

    if response.status_code in (204, 404):
        return None
    try:
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise AnimatedArtworkError(f"invalid artwork API response: {exc}") from exc

    if not isinstance(payload, dict):
        raise AnimatedArtworkError("artwork API response must be a JSON object")
    stream_url = payload.get("url")
    if stream_url is None:
        return None
    if not isinstance(stream_url, str) or not _valid_stream_url(stream_url):
        raise AnimatedArtworkError("artwork API returned an unsupported stream URL")
    return stream_url


async def convert_stream_to_webp_async(
    stream_url: str,
    destination: Path,
) -> bool:
    """Convert a bounded HLS stream to animated WebP, without replacing files."""
    if not _valid_stream_url(stream_url):
        raise AnimatedArtworkError("refusing an unsupported artwork stream URL")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise AnimatedArtworkError("ffmpeg is required to convert animated artwork")

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=".mornye-artwork-",
        suffix=".webp",
        dir=destination.parent,
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)

    try:
        try:
            process = await asyncio.create_subprocess_exec(
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-protocol_whitelist",
                "http,https,tcp,tls,crypto",
                "-rw_timeout",
                "15000000",
                "-i",
                stream_url,
                "-t",
                str(_MAX_ARTWORK_SECONDS),
                "-an",
                "-vf",
                "fps=15,scale=1000:-2:force_original_aspect_ratio=decrease",
                "-c:v",
                "libwebp_anim",
                "-loop",
                "0",
                "-quality",
                "50",
                "-compression_level",
                "4",
                "-fs",
                str(MAX_ARTWORK_BYTES),
                "-f",
                "webp",
                str(temporary_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise AnimatedArtworkError(f"could not start ffmpeg: {exc}") from exc

        try:
            _, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=60,
            )
        except asyncio.CancelledError:
            process.kill()
            await process.communicate()
            raise
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise AnimatedArtworkError("ffmpeg timed out converting artwork") from exc

        if process.returncode != 0:
            detail = (stderr or b"").decode("utf-8", errors="replace").strip()
            raise AnimatedArtworkError(
                "ffmpeg could not convert artwork"
                + (f": {detail[:500]}" if detail else "")
            )
        if (
            not temporary_path.is_file()
            or temporary_path.stat().st_size == 0
            or temporary_path.stat().st_size > MAX_ARTWORK_BYTES
        ):
            raise AnimatedArtworkError("ffmpeg produced an invalid or oversized WebP")
        with temporary_path.open("rb") as converted:
            header = converted.read(12)
        if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WEBP":
            raise AnimatedArtworkError("ffmpeg output was not a WebP image")

        if destination.exists():
            return False
        os.replace(temporary_path, destination)
        return True
    finally:
        with contextlib.suppress(OSError):
            temporary_path.unlink()


async def download_animated_artwork_async(
    artist: str,
    album: str,
    destination: Path,
    *,
    api_url: str | None = None,
) -> bool:
    """Fetch and convert an album artwork. Returns False when no art is found."""
    if not artist.strip() or not album.strip():
        return False
    stream_url = await fetch_animated_artwork_url(artist, album, api_url=api_url)
    if not stream_url:
        return False
    return await convert_stream_to_webp_async(stream_url, destination)
