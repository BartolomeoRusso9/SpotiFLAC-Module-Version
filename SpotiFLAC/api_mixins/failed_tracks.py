"""api_mixins/failed_tracks.py — the queue panel's list of tracks still missing.

See core/failed_tracks.py for how the list is kept. This mixin is only the
adapter: read it, retry it, clear it. A retry goes through the same path as
any other download (`_start_download_job`), so in --web mode it is written
down first and survives a restart just like the download it is retrying.

Multi-user mode needs nothing extra: each account has its own Api instance
with its own `owner`, and the list is keyed by owner.
"""

from __future__ import annotations


class FailedTracksMixin:
    def get_failed_tracks(self) -> dict:
        """Every track that failed and has not downloaded since. Never raises."""
        from ..core import failed_tracks

        tracks = failed_tracks.list_for(getattr(self, "owner", "") or "")
        return {"tracks": tracks, "count": len(tracks)}

    def retry_failed_tracks(
        self, config: dict | None = None, keys: list[str] | None = None
    ) -> dict:
        """Downloads the listed failures again — all of them, or only `keys`.

        One job per source collection (see failed_tracks.retry_groups). A row
        stays in the list until its track actually downloads: a retry that
        fails again only raises the attempt count, it does not lose the track.
        """
        from ..core import failed_tracks

        groups = failed_tracks.retry_groups(getattr(self, "owner", "") or "", keys)
        queued = 0
        for source_url, tracks in groups:
            self._start_download_job(
                {
                    "indices": list(range(len(tracks))),
                    "tracks": tracks,
                    "source_url": source_url,
                    # A retry is always a hand-picked selection, never "the
                    # whole collection": the collection is what already ran.
                    "whole": False,
                    "config": dict(config or {}),
                }
            )
            queued += len(tracks)
        if queued:
            self.log(f"Retrying {queued} failed track(s)…", "info")
        return {"queued": queued}

    def clear_failed_tracks(self, keys: list[str] | None = None) -> dict:
        """Drops failures from the list without retrying them."""
        from ..core import failed_tracks

        owner = getattr(self, "owner", "") or ""
        return {"removed": failed_tracks.clear(owner, keys)}
