"""api_mixins/subscriptions.py — "Following" GUI/web surface.

See core/subscriptions.py for what a subscription is and why a first check
watermarks rather than backfills. This mixin only adapts that module's plain
functions to the pywebview/`--web` calling convention (see
api_mixins/__init__.py): {"ok": True, ...} / {"ok": False, "error": ...},
matching add_registry()/remove_registry() since this is the same kind of
operation — adding and removing an entry from a persisted list.

`check_subscriptions()` is the one long-running call here (it talks to
Spotify for every followed artist), so it follows scan_local()'s shape: a
background thread, results delivered to the frontend via a push event, and an
immediate {"status": "started"} to the caller.
"""

from __future__ import annotations

import threading

from ..core.loop_runner import run_sync


class SubscriptionsMixin:
    def get_subscriptions(self) -> list | dict:
        """Every followed artist, with its last check and how much it has seen."""
        try:
            from ..core.subscriptions import list_all

            return [s.to_dict() for s in list_all()]
        except Exception as e:
            return {"error": str(e)}

    def add_subscription(
        self,
        url: str,
        name: str = "",
        include_groups: str | None = None,
        output_dir: str = "",
    ) -> dict:
        try:
            from ..core.subscriptions import add

            sub = add(
                url,
                name=name,
                include_groups=include_groups,
                # Falls back to whatever folder this Api instance downloads
                # into, which in multi-user mode is the caller's own — a
                # subscription must not write into someone else's directory.
                output_dir=output_dir or self.download_dir,
            )
            return {"ok": True, "subscription": sub.to_dict()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def remove_subscription(self, subscription_id: str) -> dict:
        try:
            from ..core.subscriptions import remove

            return {"ok": True, "removed": remove(subscription_id)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_subscription_enabled(self, subscription_id: str, enabled: bool) -> dict:
        try:
            from ..core.subscriptions import set_enabled

            return {"ok": True, "updated": set_enabled(subscription_id, bool(enabled))}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def reset_subscription(self, subscription_id: str) -> dict:
        """Forgets what this subscription has seen, so the whole back
        catalogue counts as new on the next check with backfill on.
        """
        try:
            from ..core.subscriptions import forget_seen

            forget_seen(subscription_id)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def check_subscriptions(self, download: bool = False) -> dict:
        """Checks every enabled subscription in the background.

        `download=False` (the default) only reports what is new — the same
        "show me before you fetch it" step the local-tagging scan offers,
        and for the same reason: a discography's worth of downloads should
        be something you agreed to.
        """
        threading.Thread(
            target=self._check_subscriptions_thread,
            args=(bool(download),),
            daemon=True,
        ).start()
        return {"status": "started"}

    def _check_subscriptions_thread(self, download: bool) -> None:
        from ..core.subscriptions import check_all_async

        try:
            results = run_sync(check_all_async())
        except Exception as e:
            self.log(f"Subscription check failed: {e}", "error")
            self._push("subscriptionsChecked", {"error": str(e)})
            return

        payload = [r.to_dict() for r in results]
        total_new = sum(len(r.new) for r in results)
        self.log(
            f"Checked {len(results)} subscription(s): {total_new} new release(s).",
            "info",
        )
        self._push("subscriptionsChecked", {"results": payload, "new": total_new})

        if not download or not total_new:
            return

        unfetched = 0
        for result in results:
            for release in result.new:
                try:
                    # Reuses the ordinary metadata+download path, so a
                    # subscription download is in every way an ordinary
                    # download — same settings, same progress events.
                    self.fetch_metadata(release.url)
                except Exception as e:
                    # Quiet, then counted: a check that covers many
                    # subscriptions can fail on many releases at once.
                    unfetched += 1
                    self.log(f"Could not fetch {release.title}: {e}", "warn-quiet")
        if unfetched:
            self.log(
                f"{unfetched} new release(s) could not be fetched — "
                "see the Logs view for which.",
                "warn",
            )
