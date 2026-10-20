"""Environment-variable compatibility for the SpotiFLAC-to-Mornye migration."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("mornye.config")
_warned_legacy_names: set[str] = set()


def env_value(name: str, default: str | None = None) -> str | None:
    """Read a canonical Mornye variable, falling back to its legacy name.

    When both names are present, the canonical value wins. Warnings identify
    variable names only; configured values may contain secrets or private paths.
    """
    if not name.startswith("MORNYE_"):
        return os.getenv(name, default)

    legacy_name = f"SPOTIFLAC_{name.removeprefix('MORNYE_')}"
    canonical_value = os.getenv(name)
    legacy_value = os.getenv(legacy_name)
    if legacy_value is not None:
        if legacy_name not in _warned_legacy_names:
            if canonical_value is None:
                logger.warning(
                    "Environment variable %s is deprecated; use %s instead.",
                    legacy_name,
                    name,
                )
            elif canonical_value != legacy_value:
                logger.warning(
                    "Ignoring deprecated %s because %s is set; remove the old "
                    "variable to avoid conflicting configuration.",
                    legacy_name,
                    name,
                )
            else:
                logger.warning(
                    "Environment variable %s is deprecated; use %s instead.",
                    legacy_name,
                    name,
                )
            _warned_legacy_names.add(legacy_name)

    if canonical_value is not None:
        return canonical_value
    if legacy_value is not None:
        return legacy_value
    return default
