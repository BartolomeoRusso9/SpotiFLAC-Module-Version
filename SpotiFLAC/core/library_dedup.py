"""core/library_dedup.py — the duplicates already sitting in a library.

`playlist_sync.dedup_key()` stops the same recording being downloaded twice
in one run. It says nothing about the library that run lands in: files
fetched months apart, from two providers, under two spellings of the same
artist, are three copies of one recording and nothing in the project ever
looked for them.

`audio_fingerprint.find_duplicate_groups()` does look, and looks at the
audio itself — but it fingerprints every file and then compares every
surviving pair, and `fingerprint_similarity()` tries every alignment offset
per pair in pure Python. On a few hundred files that is the right tool. On
eleven thousand it is not a slow answer, it is no answer.

So this module is the cheap pass, and the fingerprint is demoted to a
*verifier* of what the cheap pass already suspects:

  1. **Group by metadata.** ISRC first — it names a recording and survives
     every retag and rename — then folded artist/title for the files that
     have none, guarded by duration so a live take and the studio cut do
     not collapse into one group. This costs a header read and a tag read
     per file, and it is cached (see `_ScanCache`).
  2. **Verify acoustically, optionally.** With `verify=True` each *group*
     is fingerprinted and split where the audio disagrees. A group is a
     handful of files, so the O(n^2) comparison runs over handfuls instead
     of over the library.

Then it ranks each group so the best copy is the one that survives, and
`resolve_duplicates()` acts on that — by default into a quarantine folder
with a manifest that `restore_manifest()` can undo, because "delete 4000
files off my NAS" should not be a command's default behaviour.

Nothing here deletes anything unless it is asked to twice: `scan_*` only
reads, and `resolve_duplicates()` is a dry run until `dry_run=False`.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .isrc_utils import normalize_isrc
from .paths import cache_path
from .tagger import SUPPORTED_SUFFIXES

logger = logging.getLogger(__name__)

#: How far two durations may differ and still be called the same recording.
#: Encoder padding alone moves a transcode by a fraction of a second; a
#: radio edit or a live take moves it by a lot more.
DEFAULT_DURATION_TOLERANCE_S = 4.0

#: Passed through to audio_fingerprint when `verify=True`.
DEFAULT_SIMILARITY_THRESHOLD = 0.95

#: Directory name used for the quarantine folder when none is given. The
#: leading dot matters: the scan skips dotted directories, so quarantined
#: files are not re-found as duplicates of the copies they were split from.
TRASH_DIRNAME = ".spotiflac-duplicates"

MATCH_ISRC = "isrc"
MATCH_TAGS = "tags"
MATCH_BOTH = "both"

_NON_WORD_RE = re.compile(r"\W+", re.UNICODE)

#: Suffixes that name a *release* rather than a recording, and so must not
#: keep two copies of one recording apart. Kept deliberately short: every
#: entry added here is a way for two genuinely different recordings to be
#: called the same one, and the cost of that mistake is a deleted file.
#: "Live", "Radio Edit" and "Mono" are absent on purpose — those change the
#: audio, and the duration guard would not always catch them.
_VERSION_NOISE_RE = re.compile(
    r"""
    \s*
    [(\[-]?\s*
    (?:
        (?:\d{4}\s+)?re-?master(?:ed)?(?:\s+version)?(?:\s+\d{4})?
      | album\s+version
      | original\s+version
      | original\s+mix
      | bonus\s+track
      | single\s+version
    )
    \s*[)\]]?
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

#: Where a credit list stops being the artist and starts being the guests.
_ARTIST_SPLIT_RE = re.compile(
    r"\s*(?:;|/|,|&|\bfeat\.?\b|\bft\.?\b|\bwith\b|\bvs\.?\b)\s*",
    re.IGNORECASE,
)


def fold(value: str) -> str:
    """Casefolds and strips punctuation so near-identical text collapses.

    Same shape as playlist_sync._fold(), kept separate because this one is
    part of a public grouping key that a caller may want to reproduce.
    """
    return _NON_WORD_RE.sub(" ", (value or "").casefold()).strip()


def normalize_title(value: str, *, strip_version_noise: bool = True) -> str:
    """A title reduced to the recording it names.

    'Bohemian Rhapsody (2011 Remaster)' and 'Bohemian Rhapsody' are one
    recording sold twice; the parenthetical is a release detail. See
    _VERSION_NOISE_RE for why the list of what counts as a release detail
    is as short as it is.
    """
    text = (value or "").strip()
    if strip_version_noise:
        # Repeated: '… (Remastered) (Album Version)' is one title with two
        # suffixes, and one pass would leave the outer one behind.
        for _ in range(3):
            stripped = _VERSION_NOISE_RE.sub("", text)
            if stripped == text:
                break
            text = stripped
    return fold(text)


def normalize_artist(value: str) -> str:
    """The lead artist, folded.

    Only the first credit is kept: the same recording is filed as 'Artist',
    'Artist feat. Guest' and 'Artist, Guest' depending on who tagged it, and
    a key that includes the guests would call those three different songs.
    The cost is that two different artists sharing a lead name collide — the
    title and duration in the key are what keep that from mattering.
    """
    parts = _ARTIST_SPLIT_RE.split((value or "").strip(), maxsplit=1)
    return fold(parts[0] if parts else "")


# ─────────────────────────────────────────────────────────────
#  What one file is
# ─────────────────────────────────────────────────────────────


@dataclass
class LibraryFile:
    """One audio file, read once and then compared many times."""

    path: str
    size: int = 0
    mtime_ns: int = 0
    duration_ms: int = 0
    title: str = ""
    artist: str = ""
    album: str = ""
    isrc: str = ""
    codec: str = ""
    sample_rate: int = 0
    bits_per_sample: int = 0
    bitrate: int = 0
    lossless: bool = False
    tier: int = 0
    error: str = ""

    @property
    def duration_s(self) -> float:
        return self.duration_ms / 1000.0

    @property
    def extension(self) -> str:
        return Path(self.path).suffix.lower()

    @property
    def has_tags(self) -> bool:
        return bool(self.title and self.artist)

    def identity_key(self, *, strip_version_noise: bool = True) -> str:
        """Artist+title key, or "" when the file cannot be keyed by tags."""
        title = normalize_title(self.title, strip_version_noise=strip_version_noise)
        artist = normalize_artist(self.artist)
        return f"{artist}|{title}" if title and artist else ""

    def describe(self) -> str:
        parts = [self.codec or self.extension.lstrip(".") or "?"]
        if self.bits_per_sample:
            parts.append(f"{self.bits_per_sample}-bit")
        if self.sample_rate:
            parts.append(f"{self.sample_rate / 1000:g} kHz")
        if not self.lossless and self.bitrate:
            parts.append(f"{round(self.bitrate / 1000)} kbps")
        if self.size:
            parts.append(human_size(self.size))
        return " · ".join(parts)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "size": self.size,
            "duration_ms": self.duration_ms,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "isrc": self.isrc,
            "quality": self.describe(),
            "tier": self.tier,
            "lossless": self.lossless,
        }


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


# ─────────────────────────────────────────────────────────────
#  Reading the library, once
# ─────────────────────────────────────────────────────────────


def iter_audio_files(root: str | Path, *, recursive: bool = True) -> list[Path]:
    """Every supported audio file under `root`, dotted directories excluded.

    The exclusion is not cosmetic: the quarantine folder this module moves
    duplicates into is dotted, and a second scan that walked into it would
    report every quarantined file as a duplicate of the copy it was split
    from.
    """
    p = Path(root)
    if p.is_file():
        return [p] if p.suffix.lower() in SUPPORTED_SUFFIXES else []
    if not p.is_dir():
        return []

    found: list[Path] = []
    for current, dirs, files in os.walk(p):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        if not recursive:
            dirs[:] = []
        for name in sorted(files):
            if os.path.splitext(name)[1].lower() in SUPPORTED_SUFFIXES:
                found.append(Path(current) / name)
    return found


def read_file(path: str | Path) -> LibraryFile:
    """Everything the grouping and the ranking need, from one file.

    Never raises: a file that cannot be read comes back with `.error` set,
    the same contract local_scanner.scan_file() and library_upgrade
    .inspect_file() both keep, because one truncated file in eleven
    thousand must not end a scan.
    """
    from .library_upgrade import inspect_file
    from .tagger import read_embedded_tags

    p = Path(path)
    entry = LibraryFile(path=str(p))

    try:
        stat = p.stat()
        entry.size = int(stat.st_size)
        entry.mtime_ns = int(stat.st_mtime_ns)
    except OSError as exc:
        entry.error = f"Could not stat: {exc}"
        return entry

    quality = inspect_file(p)
    if quality.error:
        entry.error = quality.error
        return entry

    entry.codec = quality.codec
    entry.sample_rate = quality.sample_rate
    entry.bits_per_sample = quality.bits_per_sample
    entry.bitrate = quality.bitrate
    entry.lossless = quality.lossless
    entry.tier = quality.tier
    entry.duration_ms = int(round(quality.length_s * 1000))

    try:
        tags = read_embedded_tags(p, include_cover=False).tags
    except Exception as exc:
        # Headers read but tags did not: still usable, it just cannot be
        # grouped by name. Not an error for the scan's purposes.
        logger.debug("[dedup] no tags for %s: %s", p.name, exc)
        tags = {}

    entry.title = str(tags.get("TITLE", "") or "").strip()
    entry.artist = str(tags.get("ARTIST", "") or "").strip()
    entry.album = str(tags.get("ALBUM", "") or "").strip()
    entry.isrc = normalize_isrc(str(tags.get("ISRC", "") or ""))
    return entry


class _ScanCache:
    """Remembers a parse per path, so a rescan stats instead of decoding.

    Same idea as core/library_index_cache.py and two deliberate differences,
    both learned from watching a scan of a large library die halfway:

      - it **checkpoints** every `checkpoint_every` files, so a crash costs
        the files since the last checkpoint rather than the whole walk;
      - it **keeps entries this walk has not reached yet**, and prunes only
        when told the walk finished. A cache that pruned on every partial
        save would delete the very entries the interrupted walk had not got
        to, which turns a resumable scan back into a scan from zero.
    """

    _VERSION = 1
    _DIR = "library-dedup"

    def __init__(self, root: str | Path, *, checkpoint_every: int = 500) -> None:
        import hashlib

        key = hashlib.sha256(
            os.path.normcase(os.path.abspath(str(root))).encode("utf-8")
        ).hexdigest()[:32]
        self.path = cache_path(self._DIR) / f"{key}.json"
        self.checkpoint_every = max(1, checkpoint_every)
        self._entries: dict[str, dict] = {}
        self._seen: set[str] = set()
        self._dirty = 0
        self.hits = 0
        self.misses = 0
        self._load()

    def _load(self) -> None:
        import json

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as exc:
            logger.debug("[dedup] cache unreadable, rebuilding: %s", exc)
            return
        if not isinstance(raw, dict) or raw.get("version") != self._VERSION:
            return
        entries = raw.get("entries")
        if isinstance(entries, dict):
            self._entries = entries

    def get(self, path: Path) -> LibraryFile | None:
        entry = self._entries.get(str(path))
        if not isinstance(entry, dict):
            self.misses += 1
            return None
        try:
            stat = path.stat()
        except OSError:
            self.misses += 1
            return None
        if entry.get("mtime_ns") != int(stat.st_mtime_ns) or entry.get("size") != int(
            stat.st_size
        ):
            self.misses += 1
            return None
        self.hits += 1
        self._seen.add(str(path))
        known = {f.name for f in LibraryFile.__dataclass_fields__.values()}
        return LibraryFile(**{k: v for k, v in entry.items() if k in known})

    def put(self, entry: LibraryFile) -> None:
        self._entries[entry.path] = {
            f.name: getattr(entry, f.name)
            for f in LibraryFile.__dataclass_fields__.values()
        }
        self._seen.add(entry.path)
        self._dirty += 1
        if self._dirty >= self.checkpoint_every:
            self.save()

    def save(self, *, complete: bool = False) -> None:
        """Writes the cache. `complete=True` also drops what the walk never
        saw — the only moment at which "not seen" reliably means "gone".
        """

        if complete:
            self._entries = {k: v for k, v in self._entries.items() if k in self._seen}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            from .atomic_io import write_json_atomic

            write_json_atomic(
                self.path,
                {"version": self._VERSION, "entries": self._entries},
            )
            self._dirty = 0
        except Exception as exc:
            # A cache that cannot be written is a cache that is not used.
            logger.debug("[dedup] could not save cache: %s", exc)


# ─────────────────────────────────────────────────────────────
#  Groups
# ─────────────────────────────────────────────────────────────


def rank_key(entry: LibraryFile) -> tuple:
    """Sort key that puts the copy worth keeping first.

    Quality tier decides, then the numbers inside the tier, then size — two
    lossless copies of one recording differ by how much of it survived the
    encoder, and the bigger file is the safer keep. Metadata breaks the
    remaining ties (a tagged copy is worth more than an untagged one), and
    the path breaks the last of them so two runs over an unchanged library
    never disagree about which file to remove.
    """
    return (
        -entry.tier,
        -entry.sample_rate,
        -entry.bits_per_sample,
        -entry.bitrate,
        -entry.size,
        0 if entry.isrc else 1,
        0 if entry.has_tags else 1,
        0 if entry.album else 1,
        entry.path,
    )


@dataclass
class DuplicateGroup:
    """Files that are the same recording, best copy first."""

    key: str
    matched_by: str  # "isrc" | "tags" | "tags+audio"
    files: list[LibraryFile] = field(default_factory=list)

    def sort(self) -> None:
        self.files.sort(key=rank_key)

    @property
    def keeper(self) -> LibraryFile:
        return self.files[0]

    @property
    def duplicates(self) -> list[LibraryFile]:
        return self.files[1:]

    @property
    def reclaimable_bytes(self) -> int:
        return sum(f.size for f in self.duplicates)

    @property
    def label(self) -> str:
        best = self.keeper
        name = " — ".join(p for p in (best.artist, best.title) if p)
        return name or Path(best.path).name

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "matched_by": self.matched_by,
            "label": self.label,
            "count": len(self.files),
            "reclaimable_bytes": self.reclaimable_bytes,
            "keep": self.keeper.to_dict(),
            "duplicates": [f.to_dict() for f in self.duplicates],
        }


@dataclass
class LibraryStats:
    """The recap half of the report: what is in there, duplicates aside."""

    files: int = 0
    total_bytes: int = 0
    unreadable: int = 0
    missing_tags: int = 0
    missing_isrc: int = 0
    by_extension: dict[str, int] = field(default_factory=dict)
    by_tier: dict[str, int] = field(default_factory=dict)

    def observe(self, entry: LibraryFile) -> None:
        self.files += 1
        self.total_bytes += entry.size
        ext = entry.extension.lstrip(".") or "?"
        self.by_extension[ext] = self.by_extension.get(ext, 0) + 1
        if entry.error:
            self.unreadable += 1
            return
        from .library_upgrade import TIER_NAMES

        tier = TIER_NAMES.get(entry.tier, "unknown")
        self.by_tier[tier] = self.by_tier.get(tier, 0) + 1
        if not entry.has_tags:
            self.missing_tags += 1
        if not entry.isrc:
            self.missing_isrc += 1

    def to_dict(self) -> dict:
        return {
            "files": self.files,
            "total_bytes": self.total_bytes,
            "total_size": human_size(self.total_bytes),
            "unreadable": self.unreadable,
            "missing_tags": self.missing_tags,
            "missing_isrc": self.missing_isrc,
            "by_extension": dict(sorted(self.by_extension.items())),
            "by_tier": dict(sorted(self.by_tier.items())),
        }


@dataclass
class DedupReport:
    root: str
    match: str = MATCH_BOTH
    verified: bool = False
    duration_tolerance_s: float = DEFAULT_DURATION_TOLERANCE_S
    elapsed_s: float = 0.0
    stats: LibraryStats = field(default_factory=LibraryStats)
    groups: list[DuplicateGroup] = field(default_factory=list)
    #: Every file the walk read, duplicated or not. The groups are a view
    #: over a subset of these; export_sqlite() writes the lot, which is what
    #: makes the exported database an index of the library rather than only
    #: a list of its duplicates.
    files: list[LibraryFile] = field(default_factory=list)
    cache_hits: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def duplicate_files(self) -> int:
        return sum(len(g.duplicates) for g in self.groups)

    @property
    def reclaimable_bytes(self) -> int:
        return sum(g.reclaimable_bytes for g in self.groups)

    def to_dict(self, *, include_files: bool = False) -> dict:
        data = {
            "root": self.root,
            "match": self.match,
            "verified": self.verified,
            "duration_tolerance_s": self.duration_tolerance_s,
            "elapsed_s": round(self.elapsed_s, 2),
            "cache_hits": self.cache_hits,
            "library": self.stats.to_dict(),
            "groups": len(self.groups),
            "duplicate_files": self.duplicate_files,
            "reclaimable_bytes": self.reclaimable_bytes,
            "reclaimable": human_size(self.reclaimable_bytes),
            "notes": list(self.notes),
            "duplicate_groups": [g.to_dict() for g in self.groups],
        }
        if include_files:
            data["files"] = [f.to_dict() for f in self.files]
        return data

    def summary(self) -> str:
        stats = self.stats
        lines = [
            f"Scanned {stats.files} file(s) in {self.root} "
            f"({human_size(stats.total_bytes)}) in {self.elapsed_s:.1f}s:",
            f"  duplicate groups     : {len(self.groups)}",
            f"  redundant copies     : {self.duplicate_files}",
            f"  space reclaimable    : {human_size(self.reclaimable_bytes)}",
            f"  without artist/title : {stats.missing_tags}",
            f"  without ISRC         : {stats.missing_isrc}",
        ]
        if stats.unreadable:
            lines.append(f"  unreadable           : {stats.unreadable}")
        if stats.by_tier:
            tiers = ", ".join(f"{k} {v}" for k, v in sorted(stats.by_tier.items()))
            lines.append(f"  by quality           : {tiers}")
        if stats.by_extension:
            formats = ", ".join(
                f"{k} {v}" for k, v in sorted(stats.by_extension.items())
            )
            lines.append(f"  by format            : {formats}")
        if self.cache_hits:
            lines.append(f"  reused from cache    : {self.cache_hits}")
        for note in self.notes:
            lines.append(f"  note: {note}")
        return "\n".join(lines)


def _cluster_by_duration(
    entries: list[LibraryFile], tolerance_s: float
) -> list[list[LibraryFile]]:
    """Splits same-name files into runs of comparable length.

    Two recordings of one song filed under the same name — the studio cut
    and the seven-minute live version — must not end up in one group, and
    duration is the signal that survives whatever the tags say. Clusters
    grow from their first member rather than from their neighbour, so a
    long chain of near-misses cannot drag a group across the tolerance.

    A file whose duration could not be read joins the first cluster: it has
    the same artist and title as everything in the bucket, and refusing to
    group it would leave exactly the badly-tagged files this exists to find
    sitting on their own.
    """
    known = sorted(
        (e for e in entries if e.duration_ms > 0), key=lambda e: e.duration_ms
    )
    unknown = [e for e in entries if e.duration_ms <= 0]

    clusters: list[list[LibraryFile]] = []
    for entry in known:
        if clusters and (entry.duration_s - clusters[-1][0].duration_s <= tolerance_s):
            clusters[-1].append(entry)
        else:
            clusters.append([entry])

    if unknown:
        if clusters:
            clusters[0].extend(unknown)
        else:
            clusters.append(unknown)
    return clusters


def _isrc_camps(
    cluster: list[LibraryFile], *, respect_isrc: bool
) -> list[list[LibraryFile]]:
    """Splits one same-name, same-length cluster along what its ISRCs say.

    Two files carrying *different* ISRCs are two recordings as far as the
    only identifier that means anything is concerned, and no amount of
    agreement between their titles outranks that — so they are never merged
    on names alone.

    A file with no ISRC is the interesting case, and the one a real library
    is full of. It joins the cluster's single ISRC camp when there is only
    one, because that is what "the same song, one copy tagged properly" looks
    like. When the cluster holds two disagreeing camps there is no way to
    tell which of them it belongs to, so it joins neither and is left to
    group with the other untagged copies instead — the choice that can only
    cost a duplicate left behind, never a wrong file removed.
    """
    if not respect_isrc:
        return [cluster]

    camps: dict[str, list[LibraryFile]] = {}
    free: list[LibraryFile] = []
    for entry in cluster:
        if entry.isrc:
            camps.setdefault(entry.isrc, []).append(entry)
        else:
            free.append(entry)

    if not camps:
        return [free]
    if len(camps) == 1:
        only = next(iter(camps.values()))
        return [only + free]
    return [*camps.values(), free]


def group_duplicates(
    entries: list[LibraryFile],
    *,
    match: str = MATCH_BOTH,
    duration_tolerance_s: float = DEFAULT_DURATION_TOLERANCE_S,
    strip_version_noise: bool = True,
) -> list[DuplicateGroup]:
    """The grouping itself, on already-read files. Pure and testable.

    ISRC and tags are not two passes producing two lists of groups — they
    are two kinds of evidence merged into one set of them, because a file
    may only ever belong to a single group. Two passes would put the copy
    with an ISRC in one group and the untagged copy of the same recording in
    another, which is both a worse answer and a resolution asked to remove
    the same file twice.
    """
    usable = [e for e in entries if not e.error]
    parent = list(range(len(usable)))
    index_of = {id(e): i for i, e in enumerate(usable)}

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(members: list[LibraryFile]) -> None:
        first = find(index_of[id(members[0])])
        for entry in members[1:]:
            root = find(index_of[id(entry)])
            if root != first:
                parent[root] = first

    by_isrc: dict[str, list[LibraryFile]] = {}
    for entry in usable:
        if entry.isrc:
            by_isrc.setdefault(entry.isrc, []).append(entry)

    if match in (MATCH_ISRC, MATCH_BOTH):
        for members in by_isrc.values():
            if len(members) > 1:
                union(members)

    identity: dict[int, str] = {}
    if match in (MATCH_TAGS, MATCH_BOTH):
        by_name: dict[str, list[LibraryFile]] = {}
        for i, entry in enumerate(usable):
            key = entry.identity_key(strip_version_noise=strip_version_noise)
            if key:
                identity[i] = key
                by_name.setdefault(key, []).append(entry)
        for members in by_name.values():
            if len(members) < 2:
                continue
            for cluster in _cluster_by_duration(members, duration_tolerance_s):
                for camp in _isrc_camps(cluster, respect_isrc=match == MATCH_BOTH):
                    if len(camp) > 1:
                        union(camp)

    components: dict[int, list[int]] = {}
    for i in range(len(usable)):
        components.setdefault(find(i), []).append(i)

    groups: list[DuplicateGroup] = []
    for indices in components.values():
        if len(indices) < 2:
            continue
        members = [usable[i] for i in indices]

        # What the group is *called* follows the strongest evidence in it:
        # an ISRC shared by two of its files, or failing that the folded
        # artist/title every member agreed on.
        # Counted over the group, not over the library: the answer only ever
        # depends on this group's members, and walking every ISRC in the scan
        # for each group is quadratic in a big library for no gain.
        isrc_counts = Counter(m.isrc for m in members if m.isrc)
        shared_isrc = min(
            (isrc for isrc, count in isrc_counts.items() if count > 1),
            default="",
        )
        name_key = next((identity[i] for i in sorted(indices) if i in identity), "")
        isrc_covered = isrc_counts[shared_isrc] if shared_isrc else 0
        if shared_isrc and isrc_covered == len(members):
            matched_by = "isrc"
        elif shared_isrc:
            matched_by = "isrc+tags"
        else:
            matched_by = "tags"

        group = DuplicateGroup(
            key=f"isrc:{shared_isrc}" if shared_isrc else f"tags:{name_key}",
            matched_by=matched_by,
            files=members,
        )
        group.sort()
        groups.append(group)

    groups.sort(key=lambda g: (-g.reclaimable_bytes, g.key))
    return groups


def verify_groups(
    groups: list[DuplicateGroup],
    *,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    duration_tolerance_s: float = DEFAULT_DURATION_TOLERANCE_S,
    progress: Callable[[int, int, str], None] | None = None,
) -> tuple[list[DuplicateGroup], list[str]]:
    """Confirms each group against the audio, splitting where it disagrees.

    This is the expensive check applied cheaply: fingerprints are computed
    per group, never library-wide, so the comparison that does not scale is
    only ever asked about files already believed to be the same recording.

    A file that cannot be fingerprinted leaves its group rather than
    staying in it. Everything downstream of a group is a decision to remove
    files, so "unsure" has to mean "keep them all", not "assume yes".
    """
    from .audio_fingerprint import (
        AudioFingerprintError,
        can_compare,
        compute_fingerprint,
        find_duplicate_groups,
    )

    notes: list[str] = []
    if not can_compare():
        # Same posture as library_upgrade._verify_hires(): an optional
        # dependency that is missing degrades to "did not verify", never to
        # a failed scan — and never to a silent pass, which here would mean
        # deleting files on an unverified guess.
        notes.append(
            "acoustic verification skipped: needs pyacoustid, the fpcalc "
            "binary and libchromaprint (pip install 'SpotiFLAC[dedup]')"
        )
        return groups, notes

    verified: list[DuplicateGroup] = []
    skipped = 0
    total = sum(len(g.files) for g in groups)
    done = 0

    for group in groups:
        prints = []
        # Keyed by each fingerprint's own `path`, because that is precisely
        # what find_duplicate_groups() hands back. Keying by `entry.path` and
        # matching on `str(p)` assumed the string survives a round trip
        # through Path unchanged, which it does not on Windows
        # (`str(Path("/m/a.flac"))` is `"\\m\\a.flac"`) — every lookup
        # missed, so a verified group came back with no members and was
        # dropped as a singleton. Acoustic verification silently confirmed
        # nothing there.
        by_path: dict[Path, LibraryFile] = {}
        for entry in group.files:
            done += 1
            if progress is not None:
                try:
                    progress(done, total, entry.path)
                except Exception:
                    logger.debug("[dedup] progress callback raised", exc_info=True)
            try:
                fingerprint = compute_fingerprint(entry.path)
            except AudioFingerprintError as exc:
                skipped += 1
                logger.debug("[dedup] could not fingerprint %s: %s", entry.path, exc)
                continue
            prints.append(fingerprint)
            by_path[fingerprint.path] = entry

        for cluster in find_duplicate_groups(
            prints,
            duration_tolerance_s=duration_tolerance_s,
            similarity_threshold=similarity_threshold,
        ):
            members = [by_path[p] for p in cluster if p in by_path]
            if len(members) < 2:
                continue
            confirmed = DuplicateGroup(
                key=group.key, matched_by="tags+audio", files=members
            )
            confirmed.sort()
            verified.append(confirmed)

    if skipped:
        notes.append(
            f"{skipped} file(s) could not be fingerprinted and were left alone"
        )
    verified.sort(key=lambda g: (-g.reclaimable_bytes, g.key))
    return verified, notes


def scan_duplicates(
    root: str | Path,
    *,
    recursive: bool = True,
    match: str = MATCH_BOTH,
    duration_tolerance_s: float = DEFAULT_DURATION_TOLERANCE_S,
    strip_version_noise: bool = True,
    verify: bool = False,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    use_cache: bool = True,
    progress: Callable[[int, int, str], None] | None = None,
) -> DedupReport:
    """Walks `root`, reports every duplicate in it and what is in there.

    `progress(done, total, path)` is called per file, so a CLI or UI can
    show something during what on a real library is a long walk.
    """
    if match not in (MATCH_ISRC, MATCH_TAGS, MATCH_BOTH):
        msg = f"Unknown match mode {match!r}; expected one of isrc, tags, both"
        raise ValueError(msg)

    started = time.monotonic()
    report = DedupReport(
        root=str(root),
        match=match,
        duration_tolerance_s=duration_tolerance_s,
    )

    files = iter_audio_files(root, recursive=recursive)
    cache = _ScanCache(root) if use_cache else None
    entries: list[LibraryFile] = []

    for index, path in enumerate(files, start=1):
        if progress is not None:
            try:
                progress(index, len(files), str(path))
            except Exception:
                logger.debug("[dedup] progress callback raised", exc_info=True)

        entry = cache.get(path) if cache else None
        if entry is None:
            entry = read_file(path)
            if cache:
                cache.put(entry)
        entries.append(entry)
        report.stats.observe(entry)

    if cache:
        cache.save(complete=True)
        report.cache_hits = cache.hits

    report.files = entries

    report.groups = group_duplicates(
        entries,
        match=match,
        duration_tolerance_s=duration_tolerance_s,
        strip_version_noise=strip_version_noise,
    )

    if verify and report.groups:
        report.groups, notes = verify_groups(
            report.groups,
            similarity_threshold=similarity_threshold,
            duration_tolerance_s=duration_tolerance_s,
            progress=progress,
        )
        report.notes.extend(notes)
        report.verified = not any("skipped" in n for n in notes)

    report.elapsed_s = time.monotonic() - started
    return report


# ─────────────────────────────────────────────────────────────
#  Acting on the report
# ─────────────────────────────────────────────────────────────

ACTION_TRASH = "trash"
ACTION_DELETE = "delete"

#: What each action is called in a sentence, planned and done.
_VERBS = {
    ACTION_TRASH: ("Would quarantine", "Quarantined"),
    ACTION_DELETE: ("Would delete", "Deleted"),
    "restore": ("Would restore", "Restored"),
}


@dataclass
class ResolutionAction:
    """What was done, or would be done, to one redundant copy."""

    group_key: str
    path: str
    action: str  # "trash" | "delete" | "skip"
    size: int = 0
    destination: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        data = {
            "group": self.group_key,
            "path": self.path,
            "action": self.action,
            "size": self.size,
        }
        if self.destination:
            data["destination"] = self.destination
        if self.error:
            data["error"] = self.error
        return data


@dataclass
class ResolutionResult:
    root: str
    action: str
    dry_run: bool = True
    trash_dir: str = ""
    manifest_path: str = ""
    actions: list[ResolutionAction] = field(default_factory=list)

    @property
    def resolved(self) -> list[ResolutionAction]:
        return [a for a in self.actions if a.action != "skip"]

    @property
    def skipped(self) -> list[ResolutionAction]:
        return [a for a in self.actions if a.action == "skip"]

    @property
    def freed_bytes(self) -> int:
        return sum(a.size for a in self.resolved)

    def to_dict(self) -> dict:
        return {
            "root": self.root,
            "action": self.action,
            "dry_run": self.dry_run,
            "trash_dir": self.trash_dir,
            "manifest": self.manifest_path,
            "resolved": len(self.resolved),
            "skipped": len(self.skipped),
            "freed_bytes": self.freed_bytes,
            "freed": human_size(self.freed_bytes),
            "actions": [a.to_dict() for a in self.actions],
        }

    def summary(self) -> str:
        planned, done = _VERBS.get(self.action, ("Would act on", "Acted on"))
        outcome = "put back" if self.action == "restore" else "reclaimed"
        lines = [
            f"{planned if self.dry_run else done} {len(self.resolved)} file(s), "
            f"{human_size(self.freed_bytes)} {outcome}.",
        ]
        if self.skipped:
            lines.append(f"  skipped: {len(self.skipped)} (see the list above)")
        if self.manifest_path and not self.dry_run:
            lines.append(f"  manifest: {self.manifest_path}")
            if self.action == ACTION_TRASH:
                lines.append(
                    "  undo with: spotiflac --dedup-restore " f"{self.manifest_path}"
                )
        return "\n".join(lines)


def _unchanged(entry: LibraryFile) -> str:
    """ "" if the file on disk is still the one the scan measured.

    A report is a snapshot, and the thing it authorises is deletion. If the
    file moved, changed size or was rewritten since the scan, the entry no
    longer describes it and the only safe answer is to leave it alone and
    say so.
    """
    p = Path(entry.path)
    try:
        stat = p.stat()
    except FileNotFoundError:
        return "gone since the scan"
    except OSError as exc:
        return f"unreadable: {exc}"
    if entry.size and int(stat.st_size) != entry.size:
        return "changed since the scan (size)"
    if entry.mtime_ns and int(stat.st_mtime_ns) != entry.mtime_ns:
        return "changed since the scan (mtime)"
    return ""


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolved_or_none(path: Path) -> Path | None:
    """`Path.resolve()` that answers None instead of raising.

    Used where the path comes off disk or out of a manifest and may be
    unresolvable (a broken symlink loop, a name the OS refuses); callers
    treat None as "cannot be vouched for".
    """
    try:
        return path.resolve()
    except OSError:
        return None


def _free_destination(target: Path) -> Path:
    """`target`, or the first ' (n)' variant of it that does not exist.

    Two duplicates of one recording routinely share a filename — that is
    often how they became duplicates — and the quarantine folder mirrors
    the library's layout, so collisions there are expected rather than
    exceptional. Overwriting on collision would destroy the copy this
    function exists to preserve.
    """
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for n in range(2, 10000):
        candidate = target.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists():
            return candidate
    msg = f"Could not find a free name for {target}"
    raise OSError(msg)


def resolve_duplicates(
    report: DedupReport,
    *,
    action: str = ACTION_TRASH,
    trash_dir: str | Path | None = None,
    dry_run: bool = True,
    manifest_path: str | Path | None = None,
    keep_paths: set[str] | None = None,
    only_paths: set[str] | None = None,
    limit: int | None = None,
) -> ResolutionResult:
    """Removes the redundant copies a scan found, keeper always untouched.

    `action="trash"` (the default) moves them into a quarantine folder that
    mirrors the library's layout and writes a manifest, so the whole thing
    is one `restore_manifest()` away from never having happened.
    `action="delete"` unlinks them, which is not undoable and is therefore
    never the default.

    `dry_run=True` (also the default) walks every check and reports what it
    would do without touching a file — the two flags together are why
    resolving duplicates takes two deliberate decisions rather than one.

    `keep_paths` overrides the ranking: any path in it is kept and, if it is
    not already the keeper, the group's chosen keeper is removed instead.
    That is how a UI lets someone disagree with the ranking on one group
    without re-running the scan.

    `only_paths` narrows the run to the redundant copies it names, leaving
    every other group reported but untouched — what a UI's checkboxes mean.
    Absent, every duplicate in the report is acted on.
    """
    if action not in (ACTION_TRASH, ACTION_DELETE):
        msg = f"Unknown action {action!r}; expected 'trash' or 'delete'"
        raise ValueError(msg)

    root = Path(report.root).expanduser().resolve()
    trash = (
        Path(trash_dir).expanduser().resolve() if trash_dir else root / TRASH_DIRNAME
    )
    result = ResolutionResult(
        root=str(root),
        action=action,
        dry_run=dry_run,
        trash_dir=str(trash) if action == ACTION_TRASH else "",
    )
    keep_paths = {str(Path(p)) for p in (keep_paths or set())}
    selected = {str(Path(p)) for p in only_paths} if only_paths is not None else None
    moves: list[dict] = []
    removed = 0

    for group in report.groups:
        if limit is not None and removed >= limit:
            break

        # Whoever the ranking picked can be overridden per group; what may
        # not happen is a group with nothing left in it, so an override that
        # asks to remove every copy is refused rather than obeyed.
        keepers = [f for f in group.files if f.path in keep_paths] or [group.keeper]
        losers = [f for f in group.files if f.path not in {k.path for k in keepers}]
        if selected is not None:
            losers = [f for f in losers if f.path in selected]
        if not losers:
            continue

        keeper_problem = next(
            (problem for k in keepers if (problem := _unchanged(k))), ""
        )
        if keeper_problem:
            for entry in losers:
                result.actions.append(
                    ResolutionAction(
                        group_key=group.key,
                        path=entry.path,
                        action="skip",
                        size=entry.size,
                        error=f"kept copy {keeper_problem}; group left alone",
                    )
                )
            continue

        for entry in losers:
            if limit is not None and removed >= limit:
                break

            problem = _unchanged(entry)
            source = Path(entry.path)
            resolved_source = source.resolve() if not problem else source

            if not problem and not _within(resolved_source, root):
                problem = "outside the scanned folder"
            if not problem:
                # A symlink or hardlink to the copy being kept is not a
                # second copy of anything: removing it frees nothing and
                # can orphan the file that was supposed to survive.
                for keeper in keepers:
                    try:
                        if os.path.samefile(entry.path, keeper.path):
                            problem = "same file as the kept copy (link)"
                            break
                    except OSError:
                        pass

            if problem:
                result.actions.append(
                    ResolutionAction(
                        group_key=group.key,
                        path=entry.path,
                        action="skip",
                        size=entry.size,
                        error=problem,
                    )
                )
                continue

            destination = ""
            if action == ACTION_TRASH:
                relative = resolved_source.relative_to(root)
                destination = str(trash / relative)

            if not dry_run:
                try:
                    if action == ACTION_TRASH:
                        target = _free_destination(Path(destination))
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(source), str(target))
                        destination = str(target)
                        moves.append(
                            {
                                "from": str(source),
                                "to": destination,
                                "group": group.key,
                            }
                        )
                    else:
                        source.unlink()
                        moves.append(
                            {"from": str(source), "to": "", "group": group.key}
                        )
                except Exception as exc:
                    result.actions.append(
                        ResolutionAction(
                            group_key=group.key,
                            path=entry.path,
                            action="skip",
                            size=entry.size,
                            error=f"could not {action}: {exc}",
                        )
                    )
                    continue

            removed += 1
            result.actions.append(
                ResolutionAction(
                    group_key=group.key,
                    path=entry.path,
                    action=action,
                    size=entry.size,
                    destination=destination,
                )
            )

    if moves and not dry_run:
        result.manifest_path = str(
            _write_manifest(
                manifest_path,
                trash if action == ACTION_TRASH else root,
                action=action,
                root=str(root),
                moves=moves,
            )
        )
    return result


def _write_manifest(
    manifest_path: str | Path | None,
    default_dir: Path,
    *,
    action: str,
    root: str,
    moves: list[dict],
) -> Path:
    """Records what was moved, so it can be moved back.

    Written even for `delete`, where nothing can be moved back: the list of
    what a command removed from a library is worth having whether or not it
    can be undone.
    """
    from .atomic_io import write_json_atomic

    path = (
        Path(manifest_path).expanduser()
        if manifest_path
        else default_dir / f"dedup-{time.strftime('%Y%m%d-%H%M%S')}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(
        path,
        {
            "version": 1,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "action": action,
            "restorable": action == ACTION_TRASH,
            "root": root,
            "moves": moves,
        },
    )
    return path


def restore_manifest(
    manifest_path: str | Path, *, dry_run: bool = False
) -> ResolutionResult:
    """Puts back everything a `trash` run moved.

    A file whose original path is occupied again is left in the quarantine
    folder and reported, rather than overwriting whatever now lives there.
    """
    import json

    path = Path(manifest_path).expanduser()
    data = json.loads(path.read_text(encoding="utf-8"))
    if not data.get("restorable", False):
        msg = (
            f"{path} records a '{data.get('action')}' run, which removed the "
            "files outright — there is nothing to restore."
        )
        raise ValueError(msg)

    result = ResolutionResult(
        root=str(data.get("root", "")),
        action="restore",
        dry_run=dry_run,
        manifest_path=str(path),
    )
    restored_from: list[Path] = []

    # A manifest is a list of moves this code will perform without asking, so
    # both ends of every move are checked against where they are supposed to
    # be: out of the quarantine folder the manifest sits in, back into the
    # library it records. A hand-edited or swapped manifest can name any path
    # on the machine otherwise.
    quarantine_root = _resolved_or_none(path.parent)
    library_root = _resolved_or_none(Path(str(data.get("root", ""))))

    for move in data.get("moves", []):
        source = Path(str(move.get("to", "")))
        target = Path(str(move.get("from", "")))
        group = str(move.get("group", ""))
        size = 0
        try:
            size = source.stat().st_size
        except OSError:
            pass

        resolved_source = _resolved_or_none(source)
        resolved_target = _resolved_or_none(target)

        problem = ""
        if (
            quarantine_root is None
            or resolved_source is None
            or not _within(resolved_source, quarantine_root)
        ):
            problem = "not inside the quarantine folder this manifest belongs to"
        elif (
            library_root is None
            or resolved_target is None
            or not _within(resolved_target, library_root)
        ):
            problem = "would be restored outside the folder the manifest records"
        elif not source.exists():
            problem = "no longer in the quarantine folder"
        elif target.exists():
            problem = "something is back at the original path"

        if problem:
            result.actions.append(
                ResolutionAction(
                    group_key=group,
                    path=str(source),
                    action="skip",
                    size=size,
                    error=problem,
                )
            )
            continue

        if not dry_run:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(target))
            except Exception as exc:
                result.actions.append(
                    ResolutionAction(
                        group_key=group,
                        path=str(source),
                        action="skip",
                        size=size,
                        error=f"could not restore: {exc}",
                    )
                )
                continue

        result.actions.append(
            ResolutionAction(
                group_key=group,
                path=str(source),
                action="restore",
                size=size,
                destination=str(target),
            )
        )
        restored_from.append(source.parent)

    if not dry_run:
        _prune_empty_dirs(restored_from, stop_at=path.parent)

    return result


def _prune_empty_dirs(directories: list[Path], *, stop_at: Path) -> None:
    """Removes the empty folders a restore left behind in the quarantine.

    The quarantine mirrors the library's layout, so putting everything back
    leaves the mirror standing empty — which reads as "the undo did not
    work". Only empty directories are removed, and never `stop_at` itself:
    that is the quarantine root, and it still holds the manifest.
    """
    for directory in {d.resolve() for d in directories}:
        current = directory
        while current != stop_at and stop_at in current.parents:
            try:
                current.rmdir()
            except OSError:
                # Not empty, or not ours to remove. Either way, stop here:
                # everything above it holds this directory.
                break
            current = current.parent


# ─────────────────────────────────────────────────────────────
#  The scan as a database
# ─────────────────────────────────────────────────────────────

#: Bumped whenever the tables below change shape. load_report() refuses a
#: database it does not recognise rather than reading it wrong — the thing
#: read out of one of these is a list of files to delete.
DB_SCHEMA_VERSION = 1

_DB_SCHEMA = """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE groups (
    id                INTEGER PRIMARY KEY,
    key               TEXT NOT NULL,
    matched_by        TEXT NOT NULL,
    label             TEXT NOT NULL DEFAULT '',
    members           INTEGER NOT NULL,
    reclaimable_bytes INTEGER NOT NULL
);

CREATE TABLE files (
    id              INTEGER PRIMARY KEY,
    path            TEXT NOT NULL UNIQUE,
    size            INTEGER NOT NULL DEFAULT 0,
    mtime_ns        INTEGER NOT NULL DEFAULT 0,
    duration_ms     INTEGER NOT NULL DEFAULT 0,
    title           TEXT NOT NULL DEFAULT '',
    artist          TEXT NOT NULL DEFAULT '',
    album           TEXT NOT NULL DEFAULT '',
    isrc            TEXT NOT NULL DEFAULT '',
    codec           TEXT NOT NULL DEFAULT '',
    sample_rate     INTEGER NOT NULL DEFAULT 0,
    bits_per_sample INTEGER NOT NULL DEFAULT 0,
    bitrate         INTEGER NOT NULL DEFAULT 0,
    lossless        INTEGER NOT NULL DEFAULT 0,
    tier            INTEGER NOT NULL DEFAULT 0,
    error           TEXT NOT NULL DEFAULT '',
    -- NULL for the great majority of a library: a file belongs to at most
    -- one duplicate group, and most files belong to none.
    group_id        INTEGER REFERENCES groups(id),
    role            TEXT CHECK (role IN ('keep', 'duplicate'))
);

CREATE INDEX files_isrc     ON files(isrc)   WHERE isrc <> '';
CREATE INDEX files_identity ON files(artist, title);
CREATE INDEX files_group    ON files(group_id);

-- The join anyone reading this file by hand would write first.
CREATE VIEW duplicates AS
SELECT g.id                AS group_id,
       g.key               AS group_key,
       g.matched_by        AS matched_by,
       g.label             AS label,
       g.reclaimable_bytes AS reclaimable_bytes,
       f.role              AS role,
       f.path              AS path,
       f.size              AS size,
       f.artist            AS artist,
       f.title             AS title,
       f.album             AS album,
       f.isrc              AS isrc,
       f.duration_ms       AS duration_ms
FROM files f
JOIN groups g ON g.id = f.group_id;
"""

_FILE_COLUMNS = (
    "path",
    "size",
    "mtime_ns",
    "duration_ms",
    "title",
    "artist",
    "album",
    "isrc",
    "codec",
    "sample_rate",
    "bits_per_sample",
    "bitrate",
    "lossless",
    "tier",
    "error",
)


def export_sqlite(report: DedupReport, path: str | Path) -> Path:
    """Writes the whole scan to a SQLite database and returns its path.

    Every file the walk read is a row, not only the duplicated ones: what
    comes out is an index of the library that something else can read —
    another tool, another machine, a query typed into `sqlite3` — with the
    duplicate groups recorded on top of it as a `group_id` and a role.

    Written to a sibling `.partial` file and renamed into place, so an
    interrupted export leaves the previous database intact instead of a
    half-written one that still opens.
    """
    import json
    import sqlite3

    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    partial.unlink(missing_ok=True)

    connection = sqlite3.connect(str(partial))
    try:
        connection.executescript(_DB_SCHEMA)
        connection.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION}")

        from .. import __version__

        meta = {
            "schema_version": DB_SCHEMA_VERSION,
            "generator": f"SpotiFLAC {__version__}",
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "root": report.root,
            "match": report.match,
            "verified": int(report.verified),
            "duration_tolerance_s": report.duration_tolerance_s,
            "elapsed_s": round(report.elapsed_s, 2),
            "cache_hits": report.cache_hits,
            "files": report.stats.files,
            "total_bytes": report.stats.total_bytes,
            "unreadable": report.stats.unreadable,
            "missing_tags": report.stats.missing_tags,
            "missing_isrc": report.stats.missing_isrc,
            "groups": len(report.groups),
            "duplicate_files": report.duplicate_files,
            "reclaimable_bytes": report.reclaimable_bytes,
            "notes": json.dumps(report.notes),
        }
        connection.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            [(k, str(v)) for k, v in meta.items()],
        )

        connection.executemany(
            "INSERT INTO groups (id, key, matched_by, label, members, "
            "reclaimable_bytes) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    index,
                    group.key,
                    group.matched_by,
                    group.label,
                    len(group.files),
                    group.reclaimable_bytes,
                )
                for index, group in enumerate(report.groups, start=1)
            ],
        )

        membership: dict[str, tuple[int, str]] = {}
        for index, group in enumerate(report.groups, start=1):
            membership[group.keeper.path] = (index, "keep")
            for duplicate in group.duplicates:
                membership[duplicate.path] = (index, "duplicate")

        # A group's members are guaranteed to be in report.files when the
        # report came from a scan; a hand-built report may not have them,
        # and dropping them would silently export a group that is missing
        # the very copy it says to keep.
        known = {f.path for f in report.files}
        rows = list(report.files) + [
            f for g in report.groups for f in g.files if f.path not in known
        ]

        columns = ", ".join((*_FILE_COLUMNS, "group_id", "role"))
        placeholders = ", ".join("?" * (len(_FILE_COLUMNS) + 2))
        connection.executemany(
            f"INSERT OR REPLACE INTO files ({columns}) VALUES ({placeholders})",
            [
                (
                    *(
                        int(value) if isinstance(value, bool) else value
                        for value in (getattr(entry, c) for c in _FILE_COLUMNS)
                    ),
                    *membership.get(entry.path, (None, None)),
                )
                for entry in rows
            ],
        )
        connection.commit()
    finally:
        connection.close()

    os.replace(partial, target)
    return target


def load_report(path: str | Path) -> DedupReport:
    """Reads back a database written by export_sqlite().

    The point of the round trip is that the scan and the resolution need not
    happen in the same process, on the same machine, or on the same day: a
    NAS can produce the database overnight and a laptop can act on it. What
    keeps that safe is not this function but resolve_duplicates(), which
    re-checks every file against the size and mtime recorded here before
    touching it — so an index that has gone stale skips files rather than
    removing the wrong ones.
    """
    import json
    import sqlite3

    source = Path(path).expanduser()
    if not source.exists():
        msg = f"No such database: {source}"
        raise FileNotFoundError(msg)

    connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        try:
            meta = {
                row["key"]: row["value"]
                for row in connection.execute("SELECT key, value FROM meta")
            }
        except sqlite3.DatabaseError as exc:
            msg = f"{source} is not a SpotiFLAC dedup database ({exc})"
            raise ValueError(msg) from exc

        version = int(meta.get("schema_version", 0))
        if version != DB_SCHEMA_VERSION:
            msg = (
                f"{source} uses schema version {version}, this build reads "
                f"{DB_SCHEMA_VERSION}. Re-run the scan to regenerate it."
            )
            raise ValueError(msg)

        report = DedupReport(
            root=meta.get("root", ""),
            match=meta.get("match", MATCH_BOTH),
            verified=meta.get("verified", "0") in ("1", "True", "true"),
            duration_tolerance_s=float(
                meta.get("duration_tolerance_s", DEFAULT_DURATION_TOLERANCE_S)
            ),
            elapsed_s=float(meta.get("elapsed_s", 0.0) or 0.0),
            cache_hits=int(meta.get("cache_hits", 0) or 0),
        )
        try:
            report.notes = list(json.loads(meta.get("notes", "[]")))
        except ValueError:
            report.notes = []

        grouped: dict[int, list[tuple[str, LibraryFile]]] = {}
        for row in connection.execute(
            f"SELECT {', '.join(_FILE_COLUMNS)}, group_id, role FROM files ORDER BY id"
        ):
            entry = LibraryFile(
                **{
                    column: row[column]
                    for column in _FILE_COLUMNS
                    if column != "lossless"
                },
                lossless=bool(row["lossless"]),
            )
            report.files.append(entry)
            report.stats.observe(entry)
            if row["group_id"] is not None:
                grouped.setdefault(int(row["group_id"]), []).append(
                    (str(row["role"] or ""), entry)
                )

        for row in connection.execute(
            "SELECT id, key, matched_by FROM groups ORDER BY id"
        ):
            members = grouped.get(int(row["id"]), [])
            if len(members) < 2:
                # A group that lost a member is not a group any more, and
                # guessing which of the survivors was the keeper is exactly
                # the guess this module refuses to make elsewhere.
                continue
            group = DuplicateGroup(
                key=str(row["key"]),
                matched_by=str(row["matched_by"]),
                # The stored role decides, not the ranking: a database
                # written after someone overrode the keeper must resolve to
                # what they chose, not to what rank_key() would pick now.
                files=[e for role, e in members if role == "keep"]
                + [e for role, e in members if role != "keep"],
            )
            report.groups.append(group)
    finally:
        connection.close()

    return report
