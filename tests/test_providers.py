from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import sys
from datetime import datetime
from typing import TYPE_CHECKING

# --- RELATIVE IMPORT HACK ---
# We add the "parent" folder to the path, so that Python
# recognizes "SpotiFLAC" as a real package.
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)
# --------------------------------

# Import core components via "SpotiFLAC."
from SpotiFLAC.core.http import NetworkManager
from SpotiFLAC.providers.spotify_metadata import SpotifyMetadataClient

if TYPE_CHECKING:
    from SpotiFLAC.core.models import TrackMetadata

# Base configuration
TEST_TRACK_ID = "0VjIjW4GlUZAMYd2vXMi3b"  # The Weeknd - Blinding Lights
OUTPUT_DIR = "test_temp_downloads"
LOG_FILE = "provider_test_report.txt"

# Disable debug logs for a clean console output
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("test_script")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# "Protected" imports for ALL providers
# ---------------------------------------------------------------------------
_PROVIDER_IMPORT_SPECS = [
    ("amazon", ["amazon"], ["AmazonProvider"]),
    ("qobuz", ["qobuz"], ["QobuzProvider"]),
    ("soundcloud", ["soundcloud"], ["SoundCloudProvider"]),
    ("youtube", ["youtube"], ["YouTubeProvider"]),
    ("tidal", ["tidal"], ["TidalProvider"]),
    ("deezer", ["deezer"], ["DeezerProvider"]),
    ("apple", ["apple_music"], ["AppleMusicProvider", "AppleProvider"]),
    ("pandora", ["pandora"], ["PandoraProvider"]),
    ("joox", ["gdstudio", "gsstudio"], ["JooxProvider"]),
    ("netease", ["gdstudio", "gsstudio"], ["NeteaseProvider"]),
    ("migu", ["gdstudio", "gsstudio"], ["MiguProvider"]),
    ("kuwo", ["gdstudio", "gsstudio"], ["KuwoProvider"]),
]

PROVIDER_CLASSES: dict[str, type] = {}

for provider_key, module_candidates, class_candidates in _PROVIDER_IMPORT_SPECS:
    imported = False
    last_error: Exception | None = None
    tried_combinations = []

    for module_name in module_candidates:
        for class_name in class_candidates:
            tried_combinations.append(f"{module_name}.{class_name}")
            try:
                module = __import__(
                    f"SpotiFLAC.providers.{module_name}",
                    fromlist=[class_name],
                )
                provider_cls = getattr(module, class_name)
                PROVIDER_CLASSES[provider_key] = provider_cls
                imported = True
                break
            except (ImportError, AttributeError) as exc:
                last_error = exc
                continue
        if imported:
            break

    if not imported:
        pass

DOWNLOAD_ONLY_PROVIDERS = {"joox", "netease", "migu", "kuwo"}


class DownloadSuccessfullyStarted(BaseException):
    """Extends BaseException (not Exception) so that no provider's `except Exception`
    block can swallow it. It will always propagate up to test_single_provider,
    stopping the download immediately once real audio data has been received.
    """


def create_aborting_progress_cb():
    """Creates a callback that aborts the download as soon as it receives real audio data."""

    def cb(downloaded_bytes: int, total_bytes: int) -> None:
        # Wait for 16 KB to be sure it's audio and not an error JSON
        if downloaded_bytes > 16384:
            msg = "The download started successfully!"
            raise DownloadSuccessfullyStarted(msg)

    return cb


def log_result(provider_name: str, status: str, details: str = "") -> None:
    """Writes the result to screen and to the log file."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if status == "SUCCESS":
        msg = f"[✅ WORKING] {provider_name.upper()}"
    else:
        msg = f"[❌ ERROR] {provider_name.upper()} -> Details: {details}"

    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] {msg}\n")


async def _test_single_provider(provider, metadata: TrackMetadata) -> None:
    """Tests a single provider and captures its result."""
    provider.set_progress_callback(create_aborting_progress_cb())

    try:
        result = await provider.download_track_async(
            metadata=metadata,
            output_dir=OUTPUT_DIR,
            quality="LOSSLESS",
            embed_lyrics=False,
            enrich_metadata=False,
            allow_fallback=True,
        )
        # Reached only if the download completed (or failed) before the 16 KB threshold
        if result.success:
            log_result(
                provider.name,
                "SUCCESS",
                "Download completed before interruption.",
            )
        else:
            log_result(provider.name, "FAIL", str(result.error))

    except DownloadSuccessfullyStarted:
        # BaseException propagates through every except Exception in provider code
        log_result(provider.name, "SUCCESS")
    except Exception as e:
        log_result(provider.name, "FAIL", str(e))


async def main() -> None:

    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write("=== SPOTIFLAC PROVIDER TEST REPORT ===\n")
        f.write(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    spotify_client = SpotifyMetadataClient()
    try:
        test_metadata = await spotify_client.get_track_async(TEST_TRACK_ID)
    except Exception:
        return

    providers_to_test = []
    seen_classes = set()
    for provider_key, _, _ in _PROVIDER_IMPORT_SPECS:
        provider_cls = PROVIDER_CLASSES.get(provider_key)
        if provider_cls is None:
            continue
        if provider_cls in seen_classes:
            continue
        try:
            providers_to_test.append(provider_cls())
            seen_classes.add(provider_cls)
        except Exception:
            pass

    if not providers_to_test:
        return

    for provider in providers_to_test:
        await _test_single_provider(provider, test_metadata)

    with contextlib.suppress(Exception):
        shutil.rmtree(OUTPUT_DIR)

    await NetworkManager.aclose_loop_client()


if __name__ == "__main__":
    asyncio.run(main())
