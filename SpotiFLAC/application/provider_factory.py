from __future__ import annotations

import logging
from typing import Any, cast

from SpotiFLAC.core.base import BaseProvider

logger = logging.getLogger(__name__)


def build_providers_for_name(name: str, options: Any) -> list[BaseProvider]:
    """Build the installed provider implementations for one service id.

    Provider ranking belongs to ``ProviderResolver``; this factory only builds
    the runtime implementations needed by the compatibility adapter.
    """
    from SpotiFLAC.extensions.catalog import extension_id
    from SpotiFLAC.extensions.manager import ExtensionManager
    from SpotiFLAC.extensions.provider import JSExtensionProvider

    providers: list[BaseProvider] = []
    try:
        manager = ExtensionManager(
            ext_dir=options.ext_dir,
            auto_install_downloads=True,
        )
        original_ext_id = extension_id(name, manager)
        base_name = (
            original_ext_id.lower()
            .replace("-web", "")
            .replace("ext:", "")
            .replace("-py", "")
        )
        wants_explicit_js = "-web" in name.lower()
        wants_explicit_py = "-py" in name.lower()

        if not wants_explicit_js:
            py_candidate_name = manager.find_python_extension(base_name)
            if py_candidate_name:
                try:
                    from SpotiFLAC.extensions.python_provider import (
                        PythonExtensionProvider,
                    )

                    py_provider = cast(Any, PythonExtensionProvider)(
                        py_candidate_name,
                        ext_dir=options.ext_dir,
                    )
                    providers.append(py_provider)
                    logger.debug(
                        "Added Python provider candidate: %s", py_candidate_name
                    )
                except Exception as exc:
                    logger.warning(
                        "Python extension '%s' failed to initialize: %s",
                        py_candidate_name,
                        exc,
                    )

        installed_js = manager.get_installed(original_ext_id)
        installed_types = installed_js.types if installed_js is not None else []
        not_a_downloader = bool(installed_types) and not (
            installed_js is None or installed_js.is_download_provider
        )
        if not_a_downloader:
            logger.debug(
                "'%s' is installed but is not a download provider (%s); "
                "not using it to download",
                original_ext_id,
                ", ".join(installed_types),
            )
        elif not wants_explicit_py:
            try:
                js_provider = JSExtensionProvider(
                    original_ext_id,
                    ext_dir=options.ext_dir,
                    timeout_s=options.timeout_s or 180,
                )
                providers.append(js_provider)
                logger.debug("Added JS provider fallback: %s", original_ext_id)
            except Exception as exc:
                logger.debug(
                    "JS extension fallback not available for '%s': %s",
                    original_ext_id,
                    exc,
                )
    except Exception as exc:
        logger.warning("Failed to resolve providers for %s: %s", name, exc)

    return providers
