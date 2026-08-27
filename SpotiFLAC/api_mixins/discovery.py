"""api_mixins/discovery.py — "Discover registries" GUI/web surface.

See extensions/directories.py for what a *directory* is and why nothing is
bundled here either. This mixin only adapts that module's plain functions
to the pywebview/`--web` calling convention (see api_mixins/__init__.py) —
no logic of its own beyond exception -> {"ok": False, "error": ...}
translation, {"ok": True, ...} shape matching add_registry()/
remove_registry() (this is the same kind of operation: adding/removing a
URL from a persisted list) rather than scan_local()'s {"status": ...}.
"""

from __future__ import annotations


class DiscoveryMixin:
    def get_registry_directories(self) -> list | dict:
        """Lists every configured directory URL (env var, .env file, or
        added from this screen), same shape as get_registries().
        """
        try:
            from ..extensions.directories import list_directory_urls

            return list_directory_urls()
        except Exception as e:
            return {"error": str(e)}

    def add_registry_directory(self, url: str) -> dict:
        try:
            from ..extensions.directories import add_directory

            return {"ok": True, "directories": add_directory(url)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def remove_registry_directory(self, url: str) -> dict:
        try:
            from ..extensions.directories import remove_directory

            return {"ok": True, "directories": remove_directory(url)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def discover_registries(self) -> dict:
        """Fetches every configured directory and probes each registry it
        lists for reachability. Runs synchronously (small, bounded number
        of HTTP calls with a short timeout each — see extensions/
        directories.py's _TIMEOUT_S) — call it from a background thread
        on the frontend side if a directory listing many dead registries
        would otherwise stall the UI noticeably.

        Returns {directory_url: [{name, url, description, maintainer,
        health: {reachable, extension_count, error}}, ...]}. Never installs
        or adds anything — purely informational, exactly like
        run_health_check() is for lyrics providers.
        """
        try:
            from ..extensions.directories import probe_directories

            return probe_directories()
        except Exception as e:
            return {"error": str(e)}
