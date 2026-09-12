"""api_mixins/subscriptions.py — "Following" GUI/web surface.

See core/subscriptions.py for what a subscription is and why a first check
watermarks rather than backfills. This mixin only adapts that module's plain
functions to the pywebview/`--web` calling convention (see
api_mixins/__init__.py): {"ok": True, ...} / {"ok": False, "error": ...},
matching add_registry()/remove_registry() since this is the same kind of
operation — adding and removing an entry from a persisted list.

`check_subscriptions()` is the one long-running call here (it talks to
Spotify for every followed artist or playlist), so it follows scan_local()'s
shape: a background thread, results delivered to the frontend via a push
event, and an immediate {"status": "started"} to the caller.

Everything is scoped to `self.owner`: in multi-user mode each account has its
own Api instance, and one account's subscriptions — and the downloads they
trigger — are not another's. The scheduler (core/subscription_scheduler.py)
calls `_run_scheduled_check()` on the owner's instance for the same reason.
"""

from __future__ import annotations

import threading

from ..core.loop_runner import run_sync


class SubscriptionsMixin:
    def _subscription_owner(self) -> str:
        return getattr(self, "owner", "") or ""

    def _owned_subscription(self, subscription_id: str):
        """The caller's own subscription with this id, or None.

        Every method here takes an id, and in multi-user mode ids from one
        account must not act on another's rows — get_subscriptions() only
        ever shows an account its own.
        """
        from ..core.subscriptions import get

        sub = get(subscription_id)
        if sub is None or sub.owner != self._subscription_owner():
            return None
        return sub

    def _start_subscription_scheduler(self, api_for_owner=None):
        """Starts the scheduler for scheduled subscriptions, once per instance.

        `api_for_owner(owner)` is the Api instance that checks and downloads
        for that owner — multi-user --web passes its per-account registry.
        Without it (the desktop window, single-user --web) this instance does
        it all. Safe to call again: the desktop calls it on every page load.
        """
        from ..core.subscription_scheduler import SubscriptionScheduler

        existing = getattr(self, "_subscription_scheduler", None)
        if existing is not None:
            return existing

        def resolve(owner: str):
            return api_for_owner(owner) if api_for_owner is not None else self

        scheduler = SubscriptionScheduler(
            lambda sub: resolve(sub.owner)._run_scheduled_check(sub)
        )
        scheduler.start()
        self._subscription_scheduler = scheduler
        return scheduler

    def get_subscriptions(self) -> list | dict:
        """Every followed artist and playlist, with its last check and schedule."""
        try:
            from ..core.subscriptions import list_all

            return [s.to_dict() for s in list_all(owner=self._subscription_owner())]
        except Exception as e:
            return {"error": str(e)}

    def add_subscription(
        self,
        url: str,
        name: str = "",
        include_groups: str | None = None,
        output_dir: str = "",
        interval_minutes: int = 0,
        config: dict | None = None,
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
                owner=self._subscription_owner(),
                interval_minutes=interval_minutes,
                download_config=config,
            )
            return {"ok": True, "subscription": sub.to_dict()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def remove_subscription(self, subscription_id: str) -> dict:
        try:
            from ..core.subscriptions import remove

            if self._owned_subscription(subscription_id) is None:
                return {"ok": False, "error": "No such subscription."}
            return {"ok": True, "removed": remove(subscription_id)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_subscription_enabled(self, subscription_id: str, enabled: bool) -> dict:
        try:
            from ..core.subscriptions import set_enabled

            if self._owned_subscription(subscription_id) is None:
                return {"ok": False, "error": "No such subscription."}
            return {"ok": True, "updated": set_enabled(subscription_id, bool(enabled))}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def set_subscription_interval(
        self,
        subscription_id: str,
        interval_minutes: int,
        config: dict | None = None,
    ) -> dict:
        """Checks this subscription every `interval_minutes` (0: only on
        demand), downloading what is new with `config` — the page's current
        download settings, saved for checks that run with no page open.
        """
        try:
            from ..core.subscriptions import get, set_schedule

            if self._owned_subscription(subscription_id) is None:
                return {"ok": False, "error": "No such subscription."}
            set_schedule(subscription_id, interval_minutes, config)
            return {"ok": True, "subscription": get(subscription_id).to_dict()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def reset_subscription(self, subscription_id: str) -> dict:
        """Forgets what this subscription has seen, so the whole back
        catalogue counts as new on the next check with backfill on.
        """
        try:
            from ..core.subscriptions import forget_seen

            if self._owned_subscription(subscription_id) is None:
                return {"ok": False, "error": "No such subscription."}
            forget_seen(subscription_id)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def check_subscriptions(
        self, download: bool = False, config: dict | None = None
    ) -> dict:
        """Checks every enabled subscription in the background.

        `download=False` (the default) only reports what is new — the same
        "show me before you fetch it" step the local-tagging scan offers,
        and for the same reason: a discography's worth of downloads should
        be something you agreed to. `config` is the page's download settings;
        it is also saved on the subscriptions, so scheduled downloads follow
        settings changed since they were scheduled.
        """
        threading.Thread(
            target=self._check_subscriptions_thread,
            args=(bool(download), config),
            daemon=True,
        ).start()
        return {"status": "started"}

    def _check_subscriptions_thread(
        self, download: bool, config: dict | None = None
    ) -> None:
        from ..core.subscriptions import check_all_async, update_download_config

        owner = self._subscription_owner()
        try:
            if config:
                update_download_config(owner, config)
            results = run_sync(check_all_async(owner=owner))
        except Exception as e:
            self.log(f"Subscription check failed: {e}", "error")
            self._push("subscriptionsChecked", {"error": str(e)})
            return

        payload = [r.to_dict() for r in results]
        total_new = sum(len(r.new) for r in results)
        self.log(
            f"Checked {len(results)} subscription(s): {total_new} new item(s).",
            "info",
        )
        self._push("subscriptionsChecked", {"results": payload, "new": total_new})

        if download and total_new:
            self._download_subscription_results(results, config)

    def _run_scheduled_check(self, sub) -> None:
        """One subscription's turn on the scheduler: check, report, download.

        Always downloads what it finds — putting a subscription on a timer is
        the agreement the manual "Check & download" button asks for each time.
        """
        from ..core.subscriptions import check_async

        result = run_sync(check_async(sub))
        label = result.artist_name or sub.name or sub.url
        if result.error:
            self.log(f"Scheduled check of {label} failed: {result.error}", "warn-quiet")
        elif result.new:
            self.log(f"{label}: {len(result.new)} new item(s) — downloading.", "info")
        self._push(
            "subscriptionsChecked",
            {"results": [result.to_dict()], "new": len(result.new), "scheduled": True},
        )
        if result.new:
            self._download_subscription_results([result])

    def _download_subscription_results(
        self, results, config: dict | None = None
    ) -> None:
        """Queues every new item for download, on the ordinary download path.

        A playlist's new tracks go as one job, with the metadata the check
        already fetched. An artist's new releases go one job each, whole —
        each release's tracks are looked up first, which is what the GUI's
        own fetch does before a download.
        """
        unfetched = 0
        for result in results:
            sub = result.subscription
            settings = dict(config or sub.download_config or {})
            if result.new_tracks:
                self._queue_subscription_tracks(
                    result.new_tracks, sub.url, settings, whole=False
                )
                continue
            for release in result.new:
                try:
                    tracks = self._release_tracks(release.url)
                    if tracks:
                        self._queue_subscription_tracks(
                            tracks, release.url, settings, whole=True
                        )
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

    def _release_tracks(self, url: str) -> list:
        from ..core.tracklist import metadata_client_for
        from ..downloader import _call_metadata_get_url

        result = run_sync(_call_metadata_get_url(metadata_client_for(url), url))
        return list(result[1] or [])

    def _queue_subscription_tracks(
        self, tracks: list, source_url: str, config: dict, *, whole: bool
    ) -> None:
        # A self-contained job (see SpotiFLAC_API.run_download_job): queued in
        # --web mode, where it survives a restart, and run right away
        # otherwise. No "session", so it finishes as a background download —
        # no page asked for it.
        payload = {
            "indices": list(range(len(tracks))),
            "tracks": [t.model_dump(mode="json") for t in tracks],
            "source_url": source_url,
            "whole": whole,
            "config": dict(config),
        }
        queue = getattr(self, "_subscription_download_queue", None)
        if queue is None:
            self._start_download_job(payload)
            return

        # Multi-user --web: through the account-aware queue, so a scheduled
        # download is quota-checked and survives a restart like every other
        # one. `owner` rides in the payload because the queue thread has no
        # request to read it from (see webapp._run_queued_download).
        #
        # Deliberately not self._download_queue: download_tracks() submits to
        # that one and the multi-user handler calls download_tracks(), so a
        # job set on these instances would re-queue itself forever.
        owner = self._subscription_owner()
        try:
            queue.submit(owner, {**payload, "owner": owner})
        except Exception as exc:
            self.log(
                f"Could not queue {len(tracks)} new track(s) for download: {exc}",
                "error",
            )
