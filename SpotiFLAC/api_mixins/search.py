"""search.py — Searching the catalogue, for whatever is asking.

Spotify's search returns objects; every frontend wants the same four arrays
of plain dicts out of them, with both the modern and the legacy key spellings
side by side (`name`/`title`, `artists`/`artist`, `images`/`cover`) because
the GUI's JavaScript reads one set and older callers read the other.

That reshaping used to exist twice in `app.py` — once in `search_provider`
and again, line for line, in the thread `search_provider_async` starts — and
a third copy was about to appear for the TUI. It is one function now, and the
two entry points differ only in how they deliver the answer:

* `search_metadata_async()` awaits the client and returns the result, for
  anything already running on an event loop (the TUI, the web app);
* `SearchMixin.search_provider()` blocks and returns it, for pywebview, which
  cannot await;
* `SearchMixin.search_provider_async()` starts a thread and pushes the result
  into the window, which is what the desktop GUI's search box expects.

Nothing here imports pywebview, so the TUI can use it.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

EMPTY_RESULTS: dict[str, list] = {
    "tracks": [],
    "albums": [],
    "artists": [],
    "playlists": [],
}


def empty_results() -> dict[str, list]:
    """A fresh empty result set — never the module-level one, which callers
    would then be free to append to."""
    return {"tracks": [], "albums": [], "artists": [], "playlists": []}


def shape_search_results(results: dict, limit: int = 50) -> dict[str, list]:
    """Turns the client's objects into the dicts every frontend reads.

    `getattr` with a default throughout: a provider that stops returning one
    field should cost that field, not the whole search.
    """
    out = {"tracks": [], "albums": [], "artists": [], "playlists": []}

    # --- Tracks ---
    for t in results.get("tracks", [])[:limit]:
        out["tracks"].append(
            {
                "id": getattr(t, "id", ""),
                "name": getattr(t, "title", ""),  # Formato Go
                "title": getattr(t, "title", ""),  # Formato Legacy
                "type": "track",
                "artists": getattr(t, "artists", ""),  # Formato Go
                "artist": getattr(t, "artists", ""),  # Formato Legacy
                "album_name": getattr(t, "album", ""),
                "album": getattr(t, "album", ""),
                "duration_ms": getattr(t, "duration_ms", 0),
                "images": getattr(t, "cover_url", ""),  # Formato Go
                "cover": getattr(t, "cover_url", ""),  # Formato Legacy
                "external_urls": getattr(t, "external_url", ""),
                "external_url": getattr(t, "external_url", ""),
                "preview_url": getattr(t, "preview_url", ""),
                "playcount": getattr(t, "plays", ""),
                "is_explicit": getattr(t, "is_explicit", False),
                "explicit": getattr(t, "is_explicit", False),
                "isrc": getattr(t, "isrc", ""),
                "provider": "spotify",
            },
        )

    # --- Albums ---
    for a in results.get("albums", [])[:limit]:
        out["albums"].append(
            {
                "id": a.get("id", ""),
                "name": a.get("name", ""),
                "title": a.get("name", ""),
                "type": "album",
                "artists": a.get("artists", ""),
                "artist": a.get("artists", ""),
                "images": a.get("cover_url", ""),
                "cover": a.get("cover_url", ""),
                "release_date": a.get("release_date", ""),
                "external_urls": a.get("external_url", ""),
                "external_url": a.get("external_url", ""),
                "provider": "spotify",
            },
        )

    # --- Artists ---
    for art in results.get("artists", [])[:limit]:
        out["artists"].append(
            {
                "id": art.get("id", ""),
                "name": art.get("name", ""),
                "title": art.get("name", ""),
                "type": "artist",
                "images": art.get("cover_url", ""),
                "cover": art.get("cover_url", ""),
                "external_urls": art.get("external_url", ""),
                "external_url": art.get("external_url", ""),
                "provider": "spotify",
            },
        )

    # --- Playlists ---
    for p in results.get("playlists", [])[:limit]:
        out["playlists"].append(
            {
                "id": p.get("id", ""),
                "name": p.get("name", ""),
                "title": p.get("name", ""),
                "type": "playlist",
                "owner": p.get("owner", ""),
                "images": p.get("cover_url", ""),
                "cover": p.get("cover_url", ""),
                "external_urls": p.get("external_url", ""),
                "external_url": p.get("external_url", ""),
                "provider": "spotify",
            },
        )
    return out


async def search_metadata_async(query: str, limit: int = 50) -> dict[str, list]:
    """Searches and shapes, on the caller's event loop.

    The client has an async search; the sync `search()` next to it only
    exists to spin up a loop for callers that have none, so anything with a
    loop of its own should come through here instead.
    """
    if not query:
        return empty_results()

    from ..core.spotify_metadata import SpotifyMetadataClient

    client = SpotifyMetadataClient()
    results = await client.search_async(query, limit=limit)
    return shape_search_results(results, limit)


class SearchMixin:
    """The two blocking entry points the desktop GUI needs."""

    def search_provider(self, query, limit=50):
        """Search music providers (Spotify) for metadata matching `query`.

        Returns a dictionary with 4 sections: tracks, albums, artists,
        playlists (max `limit` results each).
        """
        try:
            from ..core.spotify_metadata import SpotifyMetadataClient

            client = SpotifyMetadataClient()
            return shape_search_results(client.search(query, limit=limit), limit)
        except Exception as exc:
            self.log(f"search_provider error: {exc}", "error")
            return empty_results()

    def search_provider_async(self, query, limit=50):
        """Starts the search in a thread and pushes the result to the window."""
        if not query:
            return {"status": "empty"}
        threading.Thread(
            target=self._search_provider_thread,
            args=(query, limit),
            daemon=True,
        ).start()
        return {"status": "started"}

    def _search_provider_thread(self, query, limit) -> None:
        try:
            from ..core.spotify_metadata import SpotifyMetadataClient

            client = SpotifyMetadataClient()
            out = shape_search_results(client.search(query, limit=limit), limit)
        except Exception as exc:
            self._push_quietly("app_handle_provider_search_error", str(exc))
            return
        self._push_quietly("app_handle_provider_search_results", out)

    def _push_quietly(self, event: str, payload: Any) -> None:
        """A window that has gone away must not turn a search into a crash."""
        try:
            self._push(event, payload)
        except Exception:
            logger.debug("could not push %s to the window", event, exc_info=True)
