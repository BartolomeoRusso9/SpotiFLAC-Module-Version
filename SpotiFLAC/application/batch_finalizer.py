from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class BatchFinalizer:
    """Owns filesystem cleanup and operator actions after a download batch."""

    def __init__(
        self,
        options: Any,
        completed_paths: dict[str, str],
        failed: list[tuple[str, str, str, str]],
        skipped: list[tuple[str, str]],
        total: int,
    ) -> None:
        self._options = options
        self._completed_paths = completed_paths
        self._failed = failed
        self._skipped = skipped
        self._total = total

    async def finalize(
        self, output_dir: str, initial_m4a: set[Path] | None = None
    ) -> None:
        removed = await asyncio.to_thread(
            self._remove_partial_files, output_dir, initial_m4a
        )
        if removed:
            logger.debug(
                "[downloader] Removed %d leftover partial/invalid audio file(s)",
                removed,
            )
        await self.execute_post_action(output_dir)

    def _remove_partial_files(
        self,
        output_dir: str,
        initial_m4a: set[Path] | None = None,
    ) -> int:
        root = Path(output_dir)
        if not root.exists():
            return 0
        completed_paths = {
            Path(path).resolve() for path in self._completed_paths.values() if path
        }
        preserved_m4a = initial_m4a or set()
        if self._options.resume:
            part_candidates = [
                path
                for path in root.rglob("*.part")
                if Path(str(path)[: -len(".part")]).resolve() in completed_paths
            ]
        else:
            part_candidates = list(root.rglob("*.part"))
        m4a_candidates = [
            path
            for path in root.rglob("*.m4a")
            if path.resolve() not in preserved_m4a
            and path.resolve() not in completed_paths
            and any(
                marker in path.stem.lower()
                for marker in (".tmp", ".download", ".temp", ".part")
            )
        ]
        removed = 0
        for path in part_candidates + m4a_candidates:
            if not path.is_file() or (
                path.suffix.lower() == ".m4a" and self._valid_m4a(path)
            ):
                continue
            try:
                path.unlink()
                removed += 1
            except OSError as exc:
                logger.warning(
                    "[downloader] Could not remove partial file %s: %s", path, exc
                )
        return removed

    @staticmethod
    def _valid_m4a(path: Path) -> bool:
        try:
            from mutagen.mp4 import MP4

            audio = MP4(str(path))
            return bool(audio.info and audio.info.length > 0)
        except Exception:
            return False

    async def execute_post_action(self, output_dir: str) -> None:
        action = self._options.post_download_action
        if not action or action == "none":
            return
        succeeded = self._total - len(self._failed) - len(self._skipped)
        skipped_count = len(self._skipped)
        failed_count = len(self._failed)
        if action == "open_folder":
            await self._open_folder(output_dir)
        elif action == "notify":
            body = f"{succeeded} tracks downloaded"
            if skipped_count:
                body += f", {skipped_count} skipped"
            if failed_count:
                body += f", {failed_count} failed"
            await self._notify("SpotiFLAC — Download completed", body)
        elif action == "command":
            command = self._options.post_download_command
            if not command:
                logger.warning(
                    "[post-action] action=command but post_download_command is empty"
                )
                return
            command = (
                command.replace("{folder}", self._quote_for_shell(output_dir))
                .replace("{succeeded}", str(succeeded))
                .replace("{skipped}", str(skipped_count))
                .replace("{failed}", str(failed_count))
            )
            try:
                process = await asyncio.create_subprocess_shell(command)
                await process.communicate()
                if process.returncode:
                    logger.warning(
                        "[post-action] command exited with status %s",
                        process.returncode,
                    )
            except Exception as exc:
                logger.warning("[post-action] command failed: %s", exc)
        else:
            logger.warning("[post-action] unknown action: %s", action)

    async def _notify(self, title: str, body: str) -> None:
        try:
            if sys.platform == "darwin":
                await asyncio.create_subprocess_exec(
                    "osascript",
                    "-e",
                    f'display notification "{body}" with title "{title}"',
                )
            elif sys.platform != "win32":
                await asyncio.create_subprocess_exec("notify-send", title, body)
        except Exception:
            pass

    async def _open_folder(self, path: str) -> None:
        try:
            if sys.platform == "darwin":
                await asyncio.create_subprocess_exec("open", path)
            elif sys.platform == "win32":
                await asyncio.create_subprocess_exec("explorer", os.path.normpath(path))
            else:
                await asyncio.create_subprocess_exec("xdg-open", path)
        except Exception as exc:
            logger.warning("[post-action] open_folder failed: %s", exc)

    @staticmethod
    def _quote_for_shell(value: str) -> str:
        if os.name == "nt":
            return '"' + re.sub(r'[";%!^&|<>]', "", value) + '"'
        return shlex.quote(value)
