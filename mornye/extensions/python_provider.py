"""Adapter for legacy Python `.sflx` packages."""

from __future__ import annotations

import importlib.util
import logging
import re
import sys
from typing import Any

from mornye.core.base import BaseProvider
from mornye.core.link_resolver import LinkResolver
from mornye.core.models import TrackMetadata

from .manager import ExtensionManager, InstalledExtension

logger = logging.getLogger(__name__)


def _spotify_track_url(metadata: TrackMetadata) -> str:
    """Return a Spotify track URL for a recognized URL or ID, else an empty string."""
    external_url = str(getattr(metadata, "external_url", "") or "").strip()
    match = re.search(
        r"https?://open\.spotify\.com/track/([A-Za-z0-9]{22})(?:[/?#]|$)",
        external_url,
        re.IGNORECASE,
    )
    if match:
        return f"https://open.spotify.com/track/{match.group(1)}"

    raw_id = str(getattr(metadata, "id", "") or "").strip()
    if raw_id.lower().startswith("spotify_"):
        raw_id = raw_id[8:]
    if re.fullmatch(r"[A-Za-z0-9]{22}", raw_id):
        return f"https://open.spotify.com/track/{raw_id}"
    return ""


def _native_track_id(provider: str, url: str) -> str:
    """Extract a supported provider track ID from its URL, else an empty string."""
    value = str(url or "").strip()
    if provider in {"tidal", "qobuz", "deezer"}:
        match = re.search(r"/track/(\d+)(?:[/?#]|$)", value, re.IGNORECASE)
        return match.group(1) if match else ""
    if provider == "amazon":
        match = re.search(
            r"/tracks?/(B[0-9A-Z]{9})(?:[/?#]|$)",
            value,
            re.IGNORECASE,
        )
        return match.group(1).upper() if match else ""
    return ""


async def _metadata_with_provider_hint(
    provider: BaseProvider,
    metadata: TrackMetadata,
) -> TrackMetadata:
    """Give legacy Python providers their native id without editing them.

    Python extensions predate the common ``checkAvailability`` contract.  A
    copy of Spotify metadata is therefore adapted at this boundary: Tidal
    and Amazon accept their native ids directly, Qobuz already understands a
    ``qobuz_<id>`` lookup key, and Deezer is converted from its native id to
    the ISRC its downloader consumes.
    """
    if not isinstance(metadata, TrackMetadata):
        return metadata

    provider_name = str(getattr(provider, "name", "")).removeprefix("ext:").lower()
    if provider_name not in {"tidal", "qobuz", "amazon", "deezer"}:
        return metadata
    source_url = _spotify_track_url(metadata)
    if not source_url:
        return metadata

    try:
        resolver = LinkResolver()
        target_url = await resolver.resolve_provider_url_async(
            source_url,
            provider_name,
        )
        native_id = _native_track_id(provider_name, target_url)
        if not native_id:
            return metadata

        if provider_name == "tidal":
            return metadata.model_copy(update={"id": f"tidal_{native_id}"})
        if provider_name == "qobuz":
            return metadata.model_copy(update={"isrc": f"qobuz_{native_id}"})
        if provider_name == "amazon":
            return metadata.model_copy(update={"id": native_id})

        # Deezer's Python extension intentionally validates by ISRC before
        # downloading.  Resolve the id to that ISRC here, still without any
        # extension change.
        isrc = await resolver._get_isrc_from_deezer_async(target_url)
        if isrc:
            return metadata.model_copy(update={"isrc": isrc})
    except Exception as exc:
        logger.debug(
            "[python-provider] native hint for %s unavailable: %s",
            provider_name,
            exc,
        )
    return metadata


def _module_name(ext: InstalledExtension) -> str:
    return f"mornye.extensions_plugins.{ext.name.replace('-', '_')}"


def _load(ext: InstalledExtension, name: str | None = None):
    module_name = name or _module_name(ext)
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, ext.entry_point)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load Python extension: {ext.entry_point}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        sys.modules.pop(module_name, None)
        logger.error(f"[Utilities] Error executing module {module_name}: {e}")
        raise
    return module


class PythonExtensionProvider(BaseProvider):
    """Loads a trusted Python provider extension and delegates BaseProvider calls."""

    def __new__(cls, ext_id: str, *, ext_dir: str | None = None, **kwargs: Any):
        """Instantiate an installed provider and wrap downloads with native ID hints.

        Raise ValueError if the extension is missing, or TypeError if it does
        not expose exactly one BaseProvider subclass.
        """
        manager = ExtensionManager(ext_dir=ext_dir, auto_install_downloads=False)
        try:
            manager.preload_python_modules()
        except Exception:
            pass

        base_name = (
            ext_id.replace("ext:", "").replace("-web", "").replace("-py", "").lower()
        )
        ext_name = manager.find_python_extension(base_name)
        if ext_name is None:
            raise ValueError(f"Python extension for '{ext_id}' is not installed")

        ext = manager.get_installed(ext_name)
        if ext is None:
            raise ValueError(f"Python extension for '{ext_id}' is not installed")

        module = _load(ext)
        candidates = [
            value
            for value in vars(module).values()
            if isinstance(value, type)
            and issubclass(value, BaseProvider)
            and value is not BaseProvider
        ]
        if len(candidates) != 1:
            raise TypeError(
                f"Extension '{ext_id}' must expose exactly one BaseProvider subclass"
            )

        provider = candidates[0](**kwargs)

        # Keep returning the extension's own provider type for compatibility,
        # but add the Python-only cross-catalogue bridge at the boundary.
        # This wrapper is deliberately installed on the instance: no file in
        # ~/.mornye/extensions is changed.
        original_download = provider.download_track_async

        async def download_with_provider_hint(
            metadata: TrackMetadata,
            output_dir: str,
            **download_kwargs: Any,
        ):
            """Adapt metadata with a native provider ID before delegating the download."""
            adapted = await _metadata_with_provider_hint(provider, metadata)
            return await original_download(adapted, output_dir, **download_kwargs)

        provider.download_track_async = download_with_provider_hint
        return provider
