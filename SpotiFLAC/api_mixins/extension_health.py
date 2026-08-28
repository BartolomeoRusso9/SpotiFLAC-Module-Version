"""api_mixins/extension_health.py — "why isn't it downloading?" in one screen.

`core/provider_stats.py` records an attempt against every provider the
download path tries. This turns that into the answer to the question this
project generates more support traffic than any other: *which of my
extensions is failing, and what did it say?*

Read-only, and it joins two sources the frontend would otherwise have to
correlate itself: the recorded outcomes (attempts, success rate, latency,
last error) and what `ExtensionManager` knows about each installed extension
(version, trust tier). An extension that is installed but has never been
tried shows up too — "never attempted" and "failing" look identical from the
outside otherwise, and they need very different fixes.
"""

from __future__ import annotations


class ExtensionHealthMixin:
    def get_extension_health(self) -> dict:
        """Per-extension reliability, worst first.

        Shape: {"providers": [...], "totals": {...}}. Every row carries
        `provider`, `attempts`, `success_rate`, `avg_duration_s`,
        `last_error` and — when the name matches something installed —
        `version` and `trust_tier`.
        """
        try:
            from ..core.provider_stats import health

            rows = health()
        except Exception as e:
            return {"error": str(e)}

        installed = self._installed_extension_index()
        seen = set()
        for row in rows:
            seen.add(row["provider"])
            row.update(installed.get(row["provider"], {}))

        # Installed but never tried: the row that tells someone their new
        # extension isn't broken, it just hasn't been reached yet (usually
        # because another provider earlier in --service order keeps winning).
        for name, info in installed.items():
            if name in seen:
                continue
            rows.append(
                {
                    "provider": name,
                    "attempts": 0,
                    "successes": 0,
                    "failures": 0,
                    "success_rate": None,
                    "avg_duration_s": 0.0,
                    "last_duration_s": 0.0,
                    "last_outcome": "",
                    "last_success": 0.0,
                    "last_failure": 0.0,
                    "last_attempt": 0.0,
                    "last_error": "",
                    "score": 0.0,
                    **info,
                }
            )

        attempts = sum(r["attempts"] for r in rows)
        successes = sum(r["successes"] for r in rows)
        return {
            "providers": rows,
            "totals": {
                "extensions": len(rows),
                "attempts": attempts,
                "successes": successes,
                "failures": attempts - successes,
                "success_rate": (successes / attempts) if attempts else None,
            },
        }

    def _installed_extension_index(self) -> dict[str, dict]:
        """{"ext:<name>": {"version": ..., "trust_tier": ...}} for what's installed.

        Keyed by the same "ext:<name>" the download path records under (see
        JSExtensionProvider.__init__), so the two sides join without the
        frontend having to know how a provider name is built.
        """
        try:
            from ..extensions.manager import ExtensionManager

            manager = ExtensionManager()
            index: dict[str, dict] = {}
            for ext in manager.list_installed():
                info: dict = {
                    "version": getattr(ext, "version", ""),
                    "display_name": getattr(ext, "display_name", "")
                    or getattr(ext, "name", ""),
                    "installed": True,
                }
                manifest = getattr(ext, "manifest", None)
                if isinstance(manifest, dict):
                    tier = manifest.get("trust_tier") or manifest.get("trustTier")
                    if tier:
                        info["trust_tier"] = tier
                index[f"ext:{ext.name}"] = info
            return index
        except Exception:
            # The health view is worth showing without the installed-extension
            # join; losing the version column is not a reason to show nothing.
            return {}

    def reset_extension_health(self) -> dict:
        """Clears the recorded stats. Useful after fixing a broken extension,
        so a long tail of old failures stops dominating the success rate.
        """
        try:
            from ..core.loop_runner import run_sync
            from ..core.provider_stats import _scorer

            run_sync(_scorer.reset_async())
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}
