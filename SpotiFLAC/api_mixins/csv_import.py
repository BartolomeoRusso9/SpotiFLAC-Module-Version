"""api_mixins/csv_import.py — a CSV as an input to the GUI, like a link.

See core/csv_source.py for what a CSV means here and why an unconvincing
match is reported rather than downloaded. This mixin only adapts that module
to the GUI's shape (see api_mixins/__init__.py), and it does so by ending in
exactly the same place a pasted link does: `self.current_tracks` filled and a
`showTracklist` push, so the track table, the checkboxes and the download
button all work on a CSV without knowing that is what they are looking at.

The file's *contents* are what crosses the bridge, never its path. In `--web`
mode the browser reads the file locally (`FileReader`) and posts the text, so
nothing here depends on the server being able to see the user's disk — and a
remote caller cannot name a path on the host to have it opened.
"""

from __future__ import annotations

import threading

from ..core.loop_runner import run_sync

#: A CSV bigger than this is not a playlist someone assembled; refusing it
#: keeps a stray 200 MB file from being parsed in the UI process.
MAX_CSV_CHARS = 2_000_000

#: Links resolved to metadata at a time. Same reasoning as
#: csv_source.DEFAULT_CONCURRENCY: politeness, not throughput.
FETCH_CONCURRENCY = 4


class CsvImportMixin:
    def preview_csv(
        self,
        content: str,
        name: str = "",
        delimiter: str | None = None,
        min_score: float | None = None,
    ) -> dict:
        """Parses and matches a CSV, downloading nothing.

        Answers the question the user actually has in front of an unfamiliar
        export — "did it understand my file, and did it find the right
        songs?" — before anything lands on disk.
        """
        from ..core import csv_source
        from ..core.errors import SpotiflacError

        if not content or not content.strip():
            return {"ok": False, "error": "The file is empty."}
        if len(content) > MAX_CSV_CHARS:
            return {"ok": False, "error": "That file is too large to read here."}

        try:
            document = csv_source.read_text(
                content, name=name or "playlist.csv", delimiter=delimiter
            )
            resolution = run_sync(
                csv_source.resolve_rows(
                    document.rows,
                    document=document,
                    min_score=(
                        csv_source.DEFAULT_MIN_SCORE if min_score is None else min_score
                    ),
                )
            )
        except SpotiflacError as e:
            return {"ok": False, "error": e.message}
        except Exception as e:
            return {"ok": False, "error": str(e)}

        return {
            "ok": True,
            "file": document.path,
            "columns": document.columns,
            "delimiter": document.delimiter,
            "rows": len(document.rows),
            "resolved": [entry.to_dict() for entry in resolution.resolved],
            "unresolved": [entry.to_dict() for entry in resolution.unresolved],
            "urls": resolution.urls,
        }

    def fetch_csv(
        self,
        content: str,
        name: str = "",
        delimiter: str | None = None,
        min_score: float | None = None,
    ) -> dict:
        """Loads a CSV into the track list, ready to download.

        Long-running (a row that carries no link is a catalogue lookup, and
        every resolved link is a metadata fetch), so it follows scan_local()'s
        shape: a background thread, an immediate {"status": "started"}, and
        the result delivered as the same 'showTracklist' event a pasted link
        produces.
        """
        if not content or not content.strip():
            return {"status": "error", "error": "The file is empty."}
        if len(content) > MAX_CSV_CHARS:
            return {"status": "error", "error": "That file is too large to read here."}

        threading.Thread(
            target=self._fetch_csv_thread,
            args=(content, name, delimiter, min_score),
            daemon=True,
        ).start()
        return {"status": "started"}

    def _fetch_csv_thread(
        self,
        content: str,
        name: str,
        delimiter: str | None,
        min_score: float | None,
    ) -> None:
        from ..core import csv_source
        from ..core.errors import SpotiflacError

        try:
            self.set_progress("Reading the file…")
            document = csv_source.read_text(
                content, name=name or "playlist.csv", delimiter=delimiter
            )
            self.log(
                f"{document.path}: {len(document.rows)} row(s). Matching them…",
                "info",
            )
            resolution = run_sync(
                csv_source.resolve_rows(
                    document.rows,
                    document=document,
                    min_score=(
                        csv_source.DEFAULT_MIN_SCORE if min_score is None else min_score
                    ),
                )
            )
        except SpotiflacError as e:
            self.log(f"CSV: {e.message}", "error")
            self.set_progress("Error.")
            self._push_safe("app_csv_error", {"error": e.message})
            return
        except Exception as e:
            self.log(f"CSV: {e}", "error")
            self.set_progress("Error.")
            self._push_safe("app_csv_error", {"error": str(e)})
            return

        for entry in resolution.unresolved:
            # Named individually rather than counted: the point of reporting
            # a miss is that the user can go and fix that row.
            self.log(f"No match for line {entry.row.line}: {entry.row.label}", "error")

        if not resolution.urls:
            self.log("Nothing in that file could be matched.", "error")
            self.set_progress("")
            self._push_safe(
                "app_csv_error", {"error": "No row could be matched to a track."}
            )
            return

        self.set_progress(f"Fetching {len(resolution.urls)} track(s)…")
        try:
            tracks = run_sync(self._csv_tracks_async(resolution.urls))
        except Exception as e:
            self.log(f"CSV: {e}", "error")
            self.set_progress("Error.")
            self._push_safe("app_csv_error", {"error": str(e)})
            return

        if not tracks:
            self.log("None of the matched links could be fetched.", "error")
            self.set_progress("")
            self._push_safe("app_csv_error", {"error": "No track could be fetched."})
            return

        self.current_tracks = tracks
        # A CSV is not a URL, and `_download_task` uses `current_url` as the
        # "download the whole thing" shortcut — blanking it makes the run go
        # track by track, which is what a list of unrelated songs is.
        self.current_url = ""

        self.set_metadata(
            document.path,
            "",
            getattr(tracks[0], "cover_url", "") or "",
            f"CSV — {len(tracks)} tracks",
            track_count=len(tracks),
            source="CSV",
        )
        self.log(
            f"{len(tracks)} track(s) ready"
            + (
                f" · {len(resolution.unresolved)} row(s) unmatched"
                if resolution.unresolved
                else ""
            ),
            "ok",
        )
        self.set_progress("Ready for download.")
        self._push_safe("showTracklist", _tracklist(tracks))
        self._push_safe(
            "app_csv_loaded",
            {
                "file": document.path,
                "rows": len(document.rows),
                "tracks": len(tracks),
                "unresolved": [entry.to_dict() for entry in resolution.unresolved],
            },
        )

    async def _csv_tracks_async(self, urls: list[str]) -> list:
        """Metadata for every resolved link, in file order.

        Runs through the downloader's own resolution so a CSV can mix
        services exactly like the address bar does — a Tidal link and an
        Apple Music link next to the Spotify ones.
        """
        import asyncio

        from ..downloader import DownloadOptions, SpotiflacDownloader

        downloader = SpotiflacDownloader(DownloadOptions(output_dir=self.download_dir))
        semaphore = asyncio.Semaphore(FETCH_CONCURRENCY)

        async def _one(url: str) -> list:
            async with semaphore:
                try:
                    _name, tracks, _info = await downloader._resolve_metadata_async(url)
                    return tracks or []
                except Exception as e:
                    self.log(f"CSV: {url} — {e}", "error")
                    return []

        groups = await asyncio.gather(*(_one(url) for url in urls))
        return [track for group in groups for track in group]

    def _push_safe(self, event: str, payload) -> None:
        try:
            self._push(event, payload)
        except Exception:
            pass


def _tracklist(tracks: list) -> list[dict]:
    """The same rows `fetch_metadata` sends, so the table needs no new case."""
    return [
        {
            "index": index,
            "id": getattr(track, "id", ""),
            "title": getattr(track, "title", f"Track {index + 1}"),
            "artist": getattr(track, "artists", "Unknown"),
            "album": getattr(track, "album", "—"),
            "cover": getattr(track, "cover_url", ""),
            "duration_ms": getattr(track, "duration_ms", 0),
            "explicit": getattr(track, "is_explicit", False),
            "isrc": getattr(track, "isrc", ""),
            "external_url": getattr(track, "external_url", ""),
            "preview_url": getattr(track, "preview_url", ""),
            "playcount": "",
            "release_date": getattr(track, "release_date", ""),
            "copyright": getattr(track, "copyright", ""),
        }
        for index, track in enumerate(tracks)
    ]
