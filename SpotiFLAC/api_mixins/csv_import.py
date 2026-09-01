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
import time

from ..core.loop_runner import run_sync

#: A CSV bigger than this is not a playlist someone assembled; refusing it
#: keeps a stray 200 MB file from being parsed in the UI process.
MAX_CSV_CHARS = 2_000_000

#: Links resolved to metadata at a time. Same reasoning as
#: csv_source.DEFAULT_CONCURRENCY: politeness, not throughput.
FETCH_CONCURRENCY = 4

#: Seconds between progress pushes while matching. Every row would be one
#: bridge message per lookup — a 1875-row file's worth of them — for a
#: counter nobody can read that fast.
PROGRESS_INTERVAL_S = 0.25


def _validated_min_score(value: float | None) -> float:
    """The match floor, checked before it reaches `csv_source.resolve_rows`.

    The CLI checks this in argparse (`launcher._match_score`) and the REST
    API in its schema; the GUI bridge is the one path that took the number
    on trust. A NaN or a negative fails every comparison the same way a
    missing floor does, so an export of messy titles quietly downloads a
    wrong match under the right filename — which is the whole thing the
    threshold exists to prevent.

    Raises `ValueError`, which both callers turn into an error for the UI.
    """
    from ..core import csv_source

    if value is None:
        return csv_source.DEFAULT_MIN_SCORE
    try:
        score = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{value!r} is not a match score.") from None
    # NaN fails both comparisons, so it is rejected by the same test.
    if not 0.0 <= score <= 1.0:
        raise ValueError(
            "The match score must be a number from 0.0 (accept anything) "
            "to 1.0 (accept only an exact match)."
        )
    return score


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
            score = _validated_min_score(min_score)
        except ValueError as e:
            return {"ok": False, "error": str(e)}

        try:
            document = csv_source.read_text(
                content, name=name or "playlist.csv", delimiter=delimiter
            )
            resolution = run_sync(
                csv_source.resolve_rows(
                    document.rows,
                    document=document,
                    min_score=score,
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
        # Checked here rather than in the thread: a bad threshold is the
        # caller's mistake to hear about, and the thread's only way of
        # answering is an error event pushed at the UI after the fact.
        try:
            score = _validated_min_score(min_score)
        except ValueError as e:
            return {"status": "error", "error": str(e)}

        threading.Thread(
            target=self._fetch_csv_thread,
            args=(content, name, delimiter, score),
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
        """`fetch_csv` validates `min_score` before starting this thread; it
        is normalized again here because the method is also called directly,
        and a threshold this path took on trust is the one that would let a
        wrong match through.
        """
        from ..core import csv_source
        from ..core.errors import SpotiflacError

        try:
            score = _validated_min_score(min_score)
            self.set_progress("Reading the file…")
            document = csv_source.read_text(
                content, name=name or "playlist.csv", delimiter=delimiter
            )
            total_rows = len(document.rows)
            self.log(
                f"{document.path}: {total_rows} row(s) to find. Matching them…",
                "debug",
            )
            report_matching = self._csv_counter("matching")
            report_matching(0, total_rows, 0)
            resolution = run_sync(
                csv_source.resolve_rows(
                    document.rows,
                    document=document,
                    min_score=score,
                    on_progress=report_matching,
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

        # The headline number, stated once and plainly: how many of the rows
        # in the file turned into a track. Everything below breaks down the
        # remainder; this is the line the user is actually looking for.
        self.log(
            f"{document.path}: {len(resolution.resolved)}/{total_rows} row(s) "
            f"matched, {len(resolution.unresolved)} not found.",
            "ok" if not resolution.unresolved else "warn",
        )

        for entry in resolution.unresolved:
            # Named individually rather than counted: the point of reporting
            # a miss is that the user can go and fix that row. But "listed
            # individually" is a property of the Logs panel, not of the
            # notification corner — at "error" this raised one popup per
            # unmatched row, and a 1875-row CSV with a few hundred misses
            # buried the window in them. "error-quiet" keeps each line, and
            # keeps it red, while the toast is the single count below.
            self.log(
                f"No match for line {entry.row.line}: {entry.row.label}",
                "error-quiet",
            )
        if resolution.unresolved:
            self.log(
                f"{len(resolution.unresolved)} of {total_rows} row(s) could not "
                "be matched — see the Logs view for which.",
                "warn",
            )

        # A file that lists the same track twice is fetched once (see
        # CsvResolution.urls). Counted because it is the last unexplained
        # part of the gap between the file's line count and the table's:
        # without it the closing summary reads as if rows had gone missing.
        duplicates = len(resolution.resolved) - len(resolution.urls)
        if duplicates:
            self.log(
                f"{duplicates} matched row(s) repeat a track already in the "
                "list; each is fetched once.",
                "debug",
            )

        if not resolution.urls:
            self.log("Nothing in that file could be matched.", "error")
            self.set_progress("")
            self._push_safe(
                "app_csv_error", {"error": "No row could be matched to a track."}
            )
            return

        # The long half of a large import: 1875 rows match in seconds, then
        # every matched link is its own metadata request. Reported row by row
        # for the same reason matching is — the phase used to state its total
        # once and then say nothing for minutes, which is indistinguishable
        # from a hang.
        report_fetch = self._csv_counter("metadata")
        report_fetch(0, len(resolution.urls), 0)
        try:
            tracks, failed_links = run_sync(
                self._csv_tracks_async(resolution.urls, on_progress=report_fetch)
            )
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
        # tracks vs resolution.resolved can differ: a row can match a link
        # whose metadata then fails to fetch. Reporting both against the
        # row count is what makes that visible instead of silent.
        if failed_links:
            self.log(
                f"{failed_links} matched link(s) returned no metadata — see the "
                "Logs view for which.",
                "warn",
            )
        self.log(
            f"{len(tracks)}/{total_rows} track(s) ready"
            + (
                f" · {len(resolution.unresolved)} row(s) unmatched"
                if resolution.unresolved
                else ""
            )
            + (f" · {failed_links} link(s) without metadata" if failed_links else "")
            + (f" · {duplicates} duplicate row(s)" if duplicates else ""),
            "ok",
        )
        self.set_progress("Ready for download.")
        self._push_safe("showTracklist", _tracklist(tracks))
        self._push_safe(
            "app_csv_loaded",
            {
                "file": document.path,
                "rows": total_rows,
                "matched": len(resolution.resolved),
                "tracks": len(tracks),
                "failed": failed_links,
                "duplicates": duplicates,
                "unresolved": [entry.to_dict() for entry in resolution.unresolved],
            },
        )
        self._start_csv_playcounts(tracks)

    def _csv_counter(self, phase: str):
        """A throttled `done/total` counter for one phase of an import.

        Both phases of a large CSV are the same shape — thousands of small
        lookups behind a single line of UI — and both need to answer the
        same two questions while they run: how far along is it, and how much
        of it is actually coming back. A file 300 rows in with 40 found has
        the wrong column mapped, and "300/1875" alone would hide that until
        the run ended.

        Returns `report(done, total, found, missing=None)` — the first three
        being the signature `csv_source.resolve_rows` calls its `on_progress`
        with, and `missing` defaulting to the rest of `done`. The metadata
        phase passes it: one link can carry several tracks, so there `found`
        counts tracks and the misses have to be counted separately rather
        than subtracted. Throttled to PROGRESS_INTERVAL_S, except for the
        last call of a phase: the final counter is the one the user reads,
        so it must never be the one the throttle drops.
        """
        verb, found_word, missing_word = (
            ("Matching", "found", "not found")
            if phase == "matching"
            else ("Fetching metadata", "ready", "no metadata")
        )
        last_push = 0.0

        def report(
            done: int, total: int, found: int, missing: int | None = None
        ) -> None:
            nonlocal last_push
            now = time.monotonic()
            if done < total and (now - last_push) < PROGRESS_INTERVAL_S:
                return
            last_push = now
            if missing is None:
                missing = max(done - found, 0)
            self.set_progress(
                f"{verb} {done}/{total} · {found} {found_word}"
                + (f" · {missing} {missing_word}" if missing else "")
            )
            self._push_safe(
                "app_csv_progress",
                {
                    "phase": phase,
                    "done": done,
                    "total": total,
                    "found": found,
                    "missing": missing,
                },
            )

        return report

    def _start_csv_playcounts(self, tracks: list) -> None:
        """Fills the Playcount column in, after the table is already up.

        A CSV is an arbitrary list of tracks, so there is no album or
        playlist query to read every count from at once the way
        fetch_metadata() does — it is one lookup per track. That is far too
        slow to hold the tracklist behind, so the table is pushed first
        (above) and the counts are patched into it as they arrive. Until
        then the column reads "—", which is why it stayed empty for CSV
        imports: nothing was ever asking for it.
        """
        track_ids = [
            tid
            for tid in (getattr(t, "id", "") for t in tracks)
            if tid and len(tid) == 22  # a Spotify base62 id; other services have none
        ]
        if not track_ids:
            return

        def _work() -> None:
            try:
                from ..core.spotfetch import SpotifyWebClient

                stats = self._fetch_track_playcounts(SpotifyWebClient(), track_ids)
                if not stats:
                    return
                self._push_safe(
                    "app_update_playcounts",
                    {tid: s.get("playcount", "") for tid, s in stats.items()},
                )
                self.log(
                    f"Playcount filled in for {len(stats)} of "
                    f"{len(track_ids)} track(s).",
                    "debug",
                )
            except Exception as e:
                # Never fatal: the tracklist is already usable without it.
                self.log(f"Playcount unavailable: {e}", "debug")

        threading.Thread(target=_work, daemon=True).start()

    async def _csv_tracks_async(
        self,
        urls: list[str],
        on_progress=None,
    ) -> tuple[list, int]:
        """Metadata for every resolved link, in file order.

        Runs through the downloader's own resolution so a CSV can mix
        services exactly like the address bar does — a Tidal link and an
        Apple Music link next to the Spotify ones.

        Returns the tracks and how many links produced none, because those
        two numbers differ and the gap is invisible otherwise: a row can
        match a link whose metadata then fails, and until it was counted the
        only trace was a track list quietly shorter than the match count.
        """
        import asyncio

        from ..downloader import DownloadOptions, SpotiflacDownloader

        downloader = SpotiflacDownloader(DownloadOptions(output_dir=self.download_dir))
        semaphore = asyncio.Semaphore(FETCH_CONCURRENCY)
        total = len(urls)
        done = 0
        fetched = 0
        failed = 0

        async def _one(url: str) -> list:
            nonlocal done, fetched, failed
            async with semaphore:
                try:
                    _name, tracks, _info = await downloader._resolve_metadata_async(url)
                except Exception as e:
                    tracks = []
                    # "error-quiet" for the same reason the unmatched rows
                    # are: one popup per failing link buries the window under
                    # a long file. The count below is the one toast.
                    self.log(f"CSV: {url} — {e}", "error-quiet")
                else:
                    if not tracks:
                        self.log(
                            f"CSV: {url} — the response carried no track.",
                            "error-quiet",
                        )
                tracks = tracks or []
                if tracks:
                    fetched += len(tracks)
                else:
                    failed += 1
                done += 1
                if on_progress is not None:
                    try:
                        on_progress(done, total, fetched, failed)
                    except Exception:
                        # Same rule as resolve_rows' counter: the display
                        # must never cost the link it was reporting on.
                        pass
                return tracks

        groups = await asyncio.gather(*(_one(url) for url in urls))
        return [track for group in groups for track in group], failed

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
