"""SpotiFLAC/core/local_processor.py — Phase 3: Safe Tagger, plus the
orchestration glue used by both the CLI (--tag-local) and the GUI/web
"Fix Local Files" tab.

Tag-writing itself is NOT reimplemented here — tagger.embed_metadata_async()
already accepts an arbitrary pre-existing file_path and already strips old
tags before writing new ones (see tagger._embed_flac / _embed_id3, both call
audio.delete() first). What this module adds on top:
  - a temporary .bak backup before overwriting, restored automatically if
    anything goes wrong partway through
  - MusicBrainz enrichment by ISRC (genre, MBIDs, barcode, label, ...),
    merged into EmbedOptions.extra_tags right before the write — see
    _with_musicbrainz_tags()
  - tying local_scanner (Phase 1) + local_matcher (Phase 2) + the tagger
    together into the "scan a folder, decide what to do with each file"
    workflows used by the CLI and the API layer
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from .local_matcher import MatchCandidate, match_local_file
from .local_scanner import LocalFileInfo, scan_path
from .tagger import EmbedOptions, embed_metadata_async

if TYPE_CHECKING:
    from .models import TrackMetadata

logger = logging.getLogger(__name__)


@dataclass
class LocalScanEntry:
    """One file's full picture: what's there now, and what it could become."""

    info: LocalFileInfo
    candidates: list[MatchCandidate] = field(default_factory=list)

    @property
    def best(self) -> MatchCandidate | None:
        return self.candidates[0] if self.candidates else None

    @property
    def is_safe_match(self) -> bool:
        return bool(self.best and self.best.is_safe)


@dataclass
class RetagResult:
    file_path: str
    success: bool
    error: str = ""
    backup_path: str = ""


async def scan_and_match_async(
    path: str | Path,
    *,
    recursive: bool = True,
    candidates_per_file: int = 5,
) -> list[LocalScanEntry]:
    """Phases 1+2 combined: scan every supported file under `path`, then
    search a match for each. Matching is done concurrently (search is I/O
    bound), scanning is done up front (it's local disk I/O, already fast).
    """
    infos = await asyncio.to_thread(scan_path, path, recursive=recursive)

    import os

    concurrency_limit = min(8, (os.cpu_count() or 4))
    semaphore = asyncio.Semaphore(concurrency_limit)

    async def _match_one(info: LocalFileInfo) -> LocalScanEntry:
        if info.error:
            return LocalScanEntry(info=info, candidates=[])
        async with semaphore:
            candidates = await match_local_file(info, limit=candidates_per_file)
        return LocalScanEntry(info=info, candidates=candidates)

    return list(await asyncio.gather(*(_match_one(i) for i in infos)))


async def _with_musicbrainz_tags(
    metadata: TrackMetadata, opts: EmbedOptions
) -> EmbedOptions:
    """Looks up `metadata.isrc` on MusicBrainz and merges the resulting tags
    (genre, MBIDs, barcode, label, ...) into `opts.extra_tags`.

    Written as its own step (rather than inside embed_metadata_async) so a
    MusicBrainz outage or missing ISRC never blocks the retag — on any
    failure this just returns `opts` unchanged, same as if MusicBrainz
    enrichment had never been requested. User-supplied `opts.extra_tags`
    values still win over MusicBrainz's on a key clash.
    """
    if not metadata.isrc:
        return opts

    try:
        from .musicbrainz import fetch_mb_metadata_async, mb_result_to_tags

        mb_data = await fetch_mb_metadata_async(metadata.isrc)
        mb_tags = mb_result_to_tags(mb_data)
    except Exception as exc:
        logger.debug(
            "[local_processor] MusicBrainz enrichment failed for isrc=%s: %s",
            metadata.isrc,
            exc,
        )
        return opts

    if not mb_tags:
        return opts

    merged = {**mb_tags, **(opts.extra_tags or {})}
    return replace(opts, extra_tags=merged)


async def retag_local_file_async(
    file_path: str | Path,
    metadata: TrackMetadata,
    opts: EmbedOptions,
    *,
    backup: bool = True,
    keep_backup: bool = False,
) -> RetagResult:
    """Phase 3, Task 2 (Safety First): backs up `file_path` to a sibling
    `.bak` file before handing it to the existing tagger. If embedding fails,
    the backup is restored over the (possibly partially-written) file before
    returning, so a crash mid-write can't leave the file corrupted. On
    success the backup is deleted unless `keep_backup=True`.
    """
    path = Path(file_path)
    backup_path: Path | None = None

    if not path.exists():
        return RetagResult(str(path), success=False, error="File not found")

    if backup:
        # Find an unused backup path to avoid overwriting existing backups
        backup_path = path.with_suffix(path.suffix + ".bak")
        counter = 1
        while backup_path.exists():
            backup_path = path.with_suffix(f"{path.suffix}.bak.{counter}")
            counter += 1
        try:
            await asyncio.to_thread(shutil.copy2, path, backup_path)
        except Exception as exc:
            return RetagResult(
                str(path),
                success=False,
                error=f"Could not create backup, aborting before any write: {exc}",
            )

    opts = await _with_musicbrainz_tags(metadata, opts)

    try:
        await embed_metadata_async(path, metadata, opts)
    except Exception as exc:
        logger.warning("[local_processor] retag failed for %s: %s", path.name, exc)
        if backup_path and backup_path.exists():
            try:
                await asyncio.to_thread(shutil.copy2, backup_path, path)
                logger.info("[local_processor] restored backup for %s", path.name)
            except Exception as restore_exc:
                logger.error(
                    "[local_processor] backup restore ALSO failed for %s: %s — "
                    "original is preserved at %s, please recover it manually",
                    path.name,
                    restore_exc,
                    backup_path,
                )
                return RetagResult(
                    str(path),
                    success=False,
                    error=(
                        f"Retag failed ({exc}), AND restoring the backup also "
                        f"failed ({restore_exc}). Your original file is safe "
                        f"at {backup_path} — restore it manually."
                    ),
                    backup_path=str(backup_path),
                )
        return RetagResult(str(path), success=False, error=str(exc))

    if backup_path and backup_path.exists() and not keep_backup:
        try:
            await asyncio.to_thread(backup_path.unlink)
        except Exception:
            pass  # harmless leftover .bak, not worth failing the whole result over

    return RetagResult(
        str(path),
        success=True,
        backup_path=str(backup_path) if (backup_path and keep_backup) else "",
    )


def default_embed_options(
    *,
    embed_lyrics: bool = True,
    enrich: bool = True,
    artist_separator: str | None = None,
) -> EmbedOptions:
    """A reasonable EmbedOptions default for local re-tagging — cover art
    from the matched metadata, lyrics and MusicBrainz enrichment on, nothing
    exotic. CLI/web callers can build their own EmbedOptions instead if they
    want different behavior.

    artist_separator: pass e.g. ", " or " / " to write multiple artists as
    one joined string instead of a multi-value ARTIST field (see
    EmbedOptions.artist_separator for why — some players, notably
    Rekordbox, mangle multi-value fields).
    """
    return EmbedOptions(
        embed_lyrics=embed_lyrics,
        lyrics_providers=[
            "apple",
            "lrclib",
        ],
        enrich=enrich,
        enrich_providers=["deezer", "apple", "qobuz", "tidal", "soundcloud"],
        artist_separator=artist_separator,
    )


async def run_local_tagging_cli(
    path: str | Path,
    *,
    dry_run: bool = False,
    force: bool = False,
    backup: bool = True,
    artist_separator: str | None = None,
) -> list[RetagResult]:
    """Drives `--tag-local` end to end: scan, match, print old → new for
    every file, then either stop there (--dry-run) or apply "safe matches"
    (--force) while always leaving anything below the confidence threshold
    for manual review — this function never applies a low-confidence match
    on its own, --force only removes the *safe*-match confirmation prompt.
    """
    print(f"\n🔎 Scanning: {path}\n")
    entries = await scan_and_match_async(path)

    if not entries:
        from .local_scanner import SUPPORTED_EXTENSIONS

        exts = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        print(f"No supported audio files found ({exts}).")
        return []

    to_apply: list[tuple[LocalScanEntry, TrackMetadata]] = []

    for entry in entries:
        info = entry.info
        old_label = (
            f"{info.old_artist or '?'} - {info.old_title or Path(info.file_path).name}"
        )

        if info.error:
            print(f"  ⚠️  {Path(info.file_path).name}: {info.error}")
            continue

        best = entry.best
        if best is None:
            print(f"  ✗  {old_label}\n     → no match found")
            continue

        new_label = f"{best.metadata.first_artist} - {best.metadata.title}"
        tag = "✓ safe match" if best.is_safe else "? needs review"
        print(f"  {old_label}\n     → {new_label}  [{best.confidence:.0f}% · {tag}]")

        if best.is_safe:
            to_apply.append((entry, best.metadata))
        else:
            print(
                "     (below 90% confidence — not applied automatically even "
                "with --force; review it in the GUI/web tab instead)",
            )

    if dry_run:
        print(
            f"\n--dry-run: {len(to_apply)} file(s) would be retagged, nothing written."
        )
        return []

    if not to_apply:
        print("\nNothing to apply.")
        return []

    if not force:
        answer = (
            input(
                f"\nApply {len(to_apply)} safe match(es) now? [y/N] ",
            )
            .strip()
            .lower()
        )
        if answer not in ("y", "yes"):
            print("Cancelled — nothing written.")
            return []

    opts = default_embed_options(artist_separator=artist_separator)
    results: list[RetagResult] = []
    print()
    for entry, metadata in to_apply:
        name = Path(entry.info.file_path).name
        result = await retag_local_file_async(
            entry.info.file_path,
            metadata,
            opts,
            backup=backup,
        )
        results.append(result)
        icon = "✓" if result.success else "✗"
        detail = "" if result.success else f" — {result.error}"
        print(f"  {icon}  {name}{detail}")

    ok = sum(1 for r in results if r.success)
    print(f"\nDone: {ok}/{len(results)} file(s) retagged.")
    return results
