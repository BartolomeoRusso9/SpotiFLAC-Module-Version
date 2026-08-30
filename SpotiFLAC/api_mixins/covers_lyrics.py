"""api_mixins/covers_lyrics.py — SpotiFLAC_API's standalone cover/lyrics saving.

Extracted verbatim from app.py (see api_mixins/__init__.py for why this is a
mixin rather than a standalone class): saving a track's cover art or lyrics
as its own file, independent of a full track download — used by the GUI's
search results view (right-click / bulk actions) rather than the download
flow itself.
"""

from __future__ import annotations

import asyncio
import os
import re
import threading

import aiofiles
import httpx

from ..core.loop_runner import run_sync
from ..core.spotify_metadata import _maximize_cover_url


class CoversLyricsMixin:
    # ── Lyrics download (separate .lrc file) ──────────────────────────────────

    def download_track_lyrics(self, track_data) -> None:
        """Download and save lyrics as a separate .lrc file for a single track."""
        threading.Thread(
            target=self._download_lyrics_task,
            args=(track_data,),
            daemon=True,
        ).start()

    def _download_lyrics_task(self, track_data) -> None:
        run_sync(self._download_lyrics_task_async(track_data))

    async def _download_lyrics_task_async(self, track_data) -> None:
        try:
            title = track_data.get("title", "Unknown")
            artist = track_data.get("artist", "")
            isrc = track_data.get("isrc", "")
            dur_ms = track_data.get("duration_ms", 0)
            track_id = track_data.get("id", "")

            from ..core.lyrics import fetch_lyrics_async

            lyrics_text, provider = await fetch_lyrics_async(
                track_name=title,
                artist_name=artist,
                duration_s=dur_ms // 1000 if dur_ms else 0,
                track_id=track_id,
                isrc=isrc,
            )

            if not lyrics_text:
                self.log(f"No lyrics found for: {title}", "error")
                return

            safe_title = re.sub(r'[\\/*?:"<>|]', "", title).strip()
            safe_artist = re.sub(r'[\\/*?:"<>|]', "", artist).strip()
            filename = (
                f"{safe_artist} - {safe_title}.lrc"
                if safe_artist
                else f"{safe_title}.lrc"
            )
            out_path = os.path.join(self.download_dir, filename)

            os.makedirs(self.download_dir, exist_ok=True)
            async with aiofiles.open(out_path, "w", encoding="utf-8") as f:
                await f.write(lyrics_text)

            self.log(f"Lyrics saved: {filename} (via {provider})", "ok")

        except Exception as e:
            self.log(f"Lyrics download error: {e}", "error")

    # ── Cover download (separate .jpg file) ───────────────────────────────────

    def download_track_cover(self, track_data) -> None:
        """Download and save album cover as a separate .jpg file."""
        threading.Thread(
            target=self._download_cover_task,
            args=(track_data,),
            daemon=True,
        ).start()

    def _download_cover_task(self, track_data) -> None:
        run_sync(self._download_cover_task_async(track_data))

    async def _download_cover_task_async(self, track_data) -> None:
        track_id = ""
        try:
            if isinstance(track_data, list) and len(track_data) > 0:
                track_data = track_data[0]

            title = track_data.get("title", "Unknown")
            artist = track_data.get("artist", "")
            track_id = track_data.get("id", "")

            raw_url = track_data.get("cover") or track_data.get("images", "")
            cover_url = _maximize_cover_url(raw_url)

            if not cover_url:
                self.log(f"No cover URL available for: {title}", "error")
                self._push(
                    "app_cover_download_finished", {"id": track_id, "success": False}
                )
                return

            self.log(f"Downloading HQ cover for: {title}…", "debug")

            import httpx

            async with httpx.AsyncClient() as client:
                resp = await client.get(cover_url, timeout=15, follow_redirects=True)
                resp.raise_for_status()

            safe_title = re.sub(r'[\\/*?:"<>|]', "", title).strip()
            safe_artist = re.sub(r'[\\/*?:"<>|]', "", artist).strip()
            filename = (
                f"{safe_artist} - {safe_title}.jpg"
                if safe_artist
                else f"{safe_title}.jpg"
            )
            out_path = os.path.join(self.download_dir, filename)

            os.makedirs(self.download_dir, exist_ok=True)
            import aiofiles

            async with aiofiles.open(out_path, "wb") as f:
                await f.write(resp.content)

            self.log(f"Cover saved: {filename}", "ok")
            # Notify the frontend of success
            self._push("app_cover_download_finished", {"id": track_id, "success": True})

        except Exception as e:
            self.log(f"Cover download error: {e}", "error")
            # Notify the frontend of the error
            self._push(
                "app_cover_download_finished", {"id": track_id, "success": False}
            )

    def download_cover(self, cover_data) -> None:
        """Download and save cover with appropriate folder structure based on type."""
        threading.Thread(
            target=self._download_cover_task_typed,
            args=(cover_data,),
            daemon=True,
        ).start()

    def _download_cover_task_typed(self, cover_data) -> None:
        run_sync(self._download_cover_task_typed_async(cover_data))

    async def _download_cover_task_typed_async(self, cover_data) -> None:
        try:
            title = cover_data.get("title", "Unknown")
            artist = cover_data.get("artist", "")
            item_type = cover_data.get("type", "ALBUM").upper()

            raw_url = cover_data.get("cover", "")
            cover_url = _maximize_cover_url(raw_url)

            if not cover_url:
                self.log(f"No cover URL available for: {title}", "error")
                return

            safe_title = re.sub(r'[\\/*?:"<>|]', "", title).strip()
            safe_artist = re.sub(r'[\\/*?:"<>|]', "", artist).strip()

            async with httpx.AsyncClient() as client:
                resp = await client.get(cover_url, timeout=15, follow_redirects=True)
                resp.raise_for_status()

            if item_type == "PLAYLIST":
                folder_path = os.path.join(self.download_dir, safe_title)
                folder_display = safe_title
            elif item_type == "ARTIST":
                folder_path = os.path.join(self.download_dir, safe_artist)
                folder_display = safe_artist
            else:
                folder_path = os.path.join(self.download_dir, safe_artist, safe_title)
                folder_display = f"{safe_artist}/{safe_title}"

            os.makedirs(folder_path, exist_ok=True)
            out_path = os.path.join(folder_path, "cover.jpg")

            async with aiofiles.open(out_path, "wb") as f:
                await f.write(resp.content)

            self.log(f"Cover saved: {folder_display}/cover.jpg", "ok")
        except Exception as e:
            self.log(f"Cover download error: {e}", "error")

    def download_album_cover(self, album_data) -> None:
        """Download and save album cover with Artist/Album folder structure."""
        threading.Thread(
            target=self._download_album_cover_task,
            args=(album_data,),
            daemon=True,
        ).start()

    def _download_album_cover_task(self, album_data) -> None:
        run_sync(self._download_album_cover_task_async(album_data))

    async def _download_album_cover_task_async(self, album_data) -> None:
        try:
            title = album_data.get("title", "Unknown")
            artist = album_data.get("artist", "Unknown Artist")

            raw_url = album_data.get("cover", "")
            cover_url = _maximize_cover_url(raw_url)

            if not cover_url:
                self.log(f"No cover URL available for: {title}", "error")
                return

            self.log(f"Downloading HQ album cover: {artist} - {title}…", "debug")

            async with httpx.AsyncClient() as client:
                resp = await client.get(cover_url, timeout=15, follow_redirects=True)
                resp.raise_for_status()

            safe_artist = re.sub(r'[\\/*?:"<>|]', "", artist).strip()
            safe_album = re.sub(r'[\\/*?:"<>|]', "", title).strip()

            folder_path = os.path.join(self.download_dir, safe_artist, safe_album)
            os.makedirs(folder_path, exist_ok=True)
            out_path = os.path.join(folder_path, "cover.jpg")

            async with aiofiles.open(out_path, "wb") as f:
                await f.write(resp.content)

            self.log(f"Album cover saved: {safe_artist}/{safe_album}/cover.jpg", "ok")
        except Exception as e:
            self.log(f"Album cover download error: {e}", "error")

    # ── Bulk: cover art for every track (ASYNC ULTRA-FAST VERSION) ──
    def download_all_covers(self, tracks_data) -> None:
        threading.Thread(
            target=self._run_async_covers,
            args=(tracks_data,),
            daemon=True,
        ).start()

    def _run_async_covers(self, tracks_data) -> None:
        run_sync(self._async_download_all_covers(tracks_data))

    async def _async_download_all_covers(self, tracks_data) -> None:
        # Per-item lines below are logged as "debug": they still show up in
        # the Logs panel, but they no longer raise a toast each. A 50-track
        # playlist used to pop 50 notifications on success and one more per
        # failure; the single summary at the end carries the same
        # information, and a failure count so nothing is lost by not
        # toasting each one.
        total = len(tracks_data)
        success, skipped, failed = 0, 0, 0

        self.log(f"Saving covers for {total} tracks at warp speed…", "debug")
        os.makedirs(self.download_dir, exist_ok=True)

        async def fetch_and_save(client, track_data, idx) -> None:
            nonlocal success, skipped, failed
            title = track_data.get("title", "Unknown")
            artist = track_data.get("artist", "")

            raw_url = track_data.get("cover", "")
            cover_url = _maximize_cover_url(raw_url)

            if not cover_url:
                skipped += 1
                return

            try:
                resp = await client.get(cover_url, timeout=15)
                resp.raise_for_status()

                safe_title = re.sub(r'[\\/*?:"<>|]', "", title).strip()
                safe_artist = re.sub(r'[\\/*?:"<>|]', "", artist).strip()
                filename = (
                    f"{safe_artist} - {safe_title}.jpg"
                    if safe_artist
                    else f"{safe_title}.jpg"
                )
                out_path = os.path.join(self.download_dir, filename)

                async with aiofiles.open(out_path, "wb") as f:
                    await f.write(resp.content)

                success += 1
                self.log(f"[{idx}/{total}] HQ Cover saved: {filename}", "debug")
            except Exception as e:
                failed += 1
                self.log(f"[{idx}/{total}] Cover error for '{title}': {e}", "debug")

        # Create an async client and start all downloads together!
        async with httpx.AsyncClient(
            limits=httpx.Limits(max_connections=50), follow_redirects=True
        ) as client:
            tasks = [
                fetch_and_save(client, track, i)
                for i, track in enumerate(tracks_data, 1)
            ]
            await asyncio.gather(*tasks)

        self.log(
            f"All covers done — {success} saved, {skipped} skipped"
            + (f", {failed} failed." if failed else "."),
            "warn" if failed else "ok",
        )

    # ── Bulk: lyrics for every track (ASYNC VERSION) ──
    def download_all_lyrics(self, tracks_data) -> None:
        threading.Thread(
            target=self._run_async_lyrics,
            args=(tracks_data,),
            daemon=True,
        ).start()

    def _run_async_lyrics(self, tracks_data) -> None:
        run_sync(self._async_download_all_lyrics(tracks_data))

    async def _async_download_all_lyrics(self, tracks_data) -> None:
        import aiofiles

        from ..core.lyrics import fetch_lyrics_async

        # Same reasoning as _async_download_all_covers above: per-item lines
        # stay in the Logs panel as "debug" instead of raising one toast per
        # track, and the closing summary reports the failures.
        total = len(tracks_data)
        success, skipped, failed = 0, 0, 0

        self.log(f"Fetching lyrics for {total} tracks concurrently…", "debug")
        os.makedirs(self.download_dir, exist_ok=True)

        async def fetch_and_save_lyric(track_data, idx) -> None:
            nonlocal success, skipped, failed
            title = track_data.get("title", "Unknown")
            artist = track_data.get("artist", "")
            isrc = track_data.get("isrc", "")
            dur_ms = track_data.get("duration_ms", 0)
            track_id = track_data.get("id", "")

            try:
                lyrics_text, provider = await fetch_lyrics_async(
                    track_name=title,
                    artist_name=artist,
                    duration_s=dur_ms // 1000 if dur_ms else 0,
                    track_id=track_id,
                    isrc=isrc,
                )

                if not lyrics_text:
                    skipped += 1
                    return

                safe_title = re.sub(r'[\\/*?:"<>|]', "", title).strip()
                safe_artist = re.sub(r'[\\/*?:"<>|]', "", artist).strip()
                filename = (
                    f"{safe_artist} - {safe_title}.lrc"
                    if safe_artist
                    else f"{safe_title}.lrc"
                )
                out_path = os.path.join(self.download_dir, filename)

                async with aiofiles.open(out_path, "w", encoding="utf-8") as f:
                    await f.write(lyrics_text)

                success += 1
                self.log(
                    f"[{idx}/{total}] Lyrics saved: {filename} (via {provider})",
                    "debug",
                )
            except Exception as e:
                failed += 1
                self.log(f"[{idx}/{total}] Lyrics error for '{title}': {e}", "debug")

        # Download all lyrics concurrently
        tasks = [
            fetch_and_save_lyric(track, i) for i, track in enumerate(tracks_data, 1)
        ]
        await asyncio.gather(*tasks)

        self.log(
            f"All lyrics done — {success} saved, {skipped} skipped"
            + (f", {failed} failed." if failed else "."),
            "warn" if failed else "ok",
        )
