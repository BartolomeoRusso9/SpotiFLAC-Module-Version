"""Environment-variable access for Mornye configuration."""

from __future__ import annotations

import os


def env_value(name: str, default: str | None = None) -> str | None:
    """Read a configured environment variable with an optional default."""
    return os.getenv(name, default)
