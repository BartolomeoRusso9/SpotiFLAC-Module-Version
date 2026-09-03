<!-- Extracted verbatim from README.md. The README had grown to 76 KB
     and 87 headings, which is past the point where either GitHub or
     PyPI renders it usefully. Nothing here was reworded in the split. -->

[← Back to the README](../../README.md)

# Local Tagging

## Local Tagging

Improve your existing music library by automatically matching local audio files against Spotify metadata and applying professional-grade tags. This is useful for:

- Fixing incomplete or incorrect tags on older ripped CDs or downloads
- Enriching a library with album art, genres, BPM, ISRCs, and other metadata from Spotify and MusicBrainz
- Bulk-updating hundreds of files in a single operation

The Local Tagging system works in **three phases**:

1. **Scan** — reads all audio files in a folder, extracts their current tags, and (for files with no tags) guesses artist/title from the filename
2. **Match** — searches for each file using the extracted or guessed metadata, returns ranked candidate matches from Spotify sorted by confidence
3. **Apply** — writes the chosen metadata to each file with automatic backup

### Supported Audio Formats

Scans and tags any format that SpotiFLAC can write: FLAC, MP3, M4A/AAC, OGG Vorbis, Opus, WAV, AIFF, WMA, WavPack, Monkey's Audio, Musepack, TrueAudio.

### Using the GUI / Web Interface

Open the **"Fix Local Files"** tab and follow the wizard:

1. **Choose a folder** — browse to your music directory (or drag & drop a folder onto the interface)
2. **Review matches** — SpotiFLAC scans, matches, and displays each file with up to 5 candidate matches, sorted by confidence (0–100)
3. **Select metadata** — for each file, choose which match to apply, or skip it entirely
4. **Preview changes** — see what tags will be written before applying
5. **Apply** — apply all changes at once with progress tracking and automatic per-file backup

Files with confidence ≥ 90% are marked as "safe to auto-apply"; files below that threshold are flagged for manual review.

### Using the Python API

```python
import asyncio
from SpotiFLAC.core.local_processor import (
    scan_and_match_async,
    retag_local_file_async,
    default_embed_options,
)
from SpotiFLAC.core.models import TrackMetadata

async def fix_library(folder_path: str) -> None:
    # Phase 1 & 2: scan folder and find matches for each file
    entries = await scan_and_match_async(
        folder_path,
        recursive=True,           # scan subdirectories too
        candidates_per_file=5,    # show top 5 matches
    )

    # Phase 3: apply metadata for each file
    embed_opts = default_embed_options()
    for entry in entries:
        if entry.best and entry.is_safe_match:  # confidence >= 90%
            result = await retag_local_file_async(
                file_path=str(entry.info.file_path),
                metadata=entry.best.metadata,
                options=embed_opts,
                backup=True,  # automatic .bak backup
            )
            if result.success:
                print(f"✓ {entry.info.file_path}: tagged successfully")
            else:
                print(f"✗ {entry.info.file_path}: {result.error}")

asyncio.run(fix_library("~/Music/MyLibrary"))
```

### Match Confidence & Safety

Matching uses a **string-similarity algorithm** that compares the file's title + artist against each Spotify search result:

- **Confidence ≥ 90%** — marked as "safe" and can be auto-applied without review
- **Confidence < 90%** — flagged for manual review to avoid mislabeling

The matching algorithm is heuristic and does not analyze audio content — it compares text only. A track with unusual spelling or featuring artists can have legitimate lower scores even when the match is correct. Always review before applying in bulk if you're unsure.

### Metadata Written

When applying a match, the following tags are written (previous tags are stripped):

- Standard tags: title, artist, album, album artist, date, disc, track number, genre
- Extended metadata: ISRC, BPM, labels, lyrics (if `embed_lyrics=True`)
- Cover art: highest-resolution available from Spotify and enrichment providers
- MusicBrainz enrichment: genre, BPM, organization/label, UPC (if available)

### Backup & Recovery

Every file gets an automatic `.bak` backup before tagging:

```text
MyTrack.flac         (original)
MyTrack.flac.bak     (backup)
```

If something goes wrong during the apply step, the backup is restored and the operation is rolled back for that file. You can also delete `.bak` files manually after confirming the results are correct.

### Per-File Customization

Fine-tune embedding options for specific use cases:

```python
from SpotiFLAC.core.tagger import EmbedOptions

custom_opts = EmbedOptions(
    embed_cover=True,
    embed_lyrics=True,
    lyrics_type="lrc",
    flac_compression_level=8,
)

result = await retag_local_file_async(
    file_path="song.flac",
    metadata=matched_metadata,
    options=custom_opts,
    backup=True,
)
```

### Duplicate Detection (acoustic fingerprint)

Local Tagging's own dedup (above) matches by ISRC or by normalized title+artist text — cheap and usually right, but blind to a re-rip with wrong or missing tags, or the same recording pulled from two different providers with slightly different metadata. This is a second, independent signal that looks at the *audio itself* instead: [Chromaprint](https://acoustid.org/chromaprint) acoustic fingerprints, compared locally — no network call, no AcoustID lookup, no API key.

Off by default and fully opt-in (same posture as [Hi-Res Verification](configuration.md#hi-res-verification)): needs the optional `pyacoustid` package and the `fpcalc` binary it wraps.

```bash
pip install SpotiFLAC[dedup]
# then install fpcalc — most package managers ship it as "chromaprint" or
# "libchromaprint-tools" (see https://acoustid.org/chromaprint)

python -m SpotiFLAC.tools.dedup_check_cli ~/Music/MyLibrary
```

```text
Fingerprinting 340 file(s)…

Found 2 duplicate group(s):

Group 1 (2 files):
  - /Users/you/Music/MyLibrary/Artist - Song.flac
  - /Users/you/Music/MyLibrary/Compilation/Artist - Song (re-rip).mp3
```

Also available as a "Find Duplicates" button in the GUI's Fix Local Files tab (same folder path as a normal scan), backed by `get_dedup_status()` (whether it can run at all on this machine) and `scan_for_duplicates(path, recursive=True, threshold=0.95)` (runs in a background thread; results arrive via the `app_dedup_results` push event, `app_dedup_error` on failure — same shape as `scan_local()`), or directly in Python:

```python
from SpotiFLAC.core.audio_fingerprint import (
    compute_fingerprint, find_duplicate_groups, is_available,
)

if is_available():
    fingerprints = [compute_fingerprint(f) for f in my_files]
    for group in find_duplicate_groups(fingerprints):
        print("Duplicates:", group)
```

A duration pre-filter (`duration_tolerance_s`, default 3.0) skips the (more expensive) fingerprint comparison for any pair that couldn't plausibly match, so this stays practical for a real, varied library. Like Hi-Res Verification, treat a match as a strong hint, not a certification — review before deleting anything.

**On size:** this fingerprints every file and then compares every surviving pair, and the comparison tries every alignment offset per pair. That is the right tool for a folder of a few hundred files and the wrong one for a library of ten thousand. For a whole library, use **Library Deduplication** below, which uses the fingerprint only to confirm what the metadata already suspects.

---

## Library Deduplication

Finds the duplicate *recordings* across an entire library, reports what is in
there, and — when told to twice — resolves them.

```bash
# Report only. Always start here.
spotiflac --dedup-library ~/Music

# Move the redundant copies into a quarantine folder (undoable).
spotiflac --dedup-library ~/Music --dedup-apply

# Changed your mind:
spotiflac --dedup-restore ~/Music/.spotiflac-duplicates/dedup-20260901-011058.json
```

```text
Scanned 11342 file(s) in /Users/you/Music (412.8 GB) in 46.1s:
  duplicate groups     : 731
  redundant copies     : 902
  space reclaimable    : 21.4 GB
  without artist/title : 118
  without ISRC         : 6204
  by quality           : lossless 9871, lossy 1471
  by format            : flac 9203, m4a 668, mp3 1471

1. Lazza — Ouverture  [3 copies, 71.2 MB reclaimable, matched by isrc+tags]
    keep · /Users/you/Music/Lazza/Sirio/01 - Ouverture.flac
           flac · 24-bit · 96 kHz · 71.2 MB
    drop · /Users/you/Music/Singles/Ouverture.flac
           flac · 16-bit · 44.1 kHz · 34.9 MB
    drop · /Users/you/Music/old-phone/Ouverture.mp3
           mp3 · 44.1 kHz · 320 kbps · 8.1 MB
```

### How two files become one group

Two signals, merged into a single set of groups rather than applied as two
separate passes — a file may only ever belong to one group, or resolution
would be asked to remove it twice.

| Signal | What it does |
| --- | --- |
| **ISRC** | Names the recording itself and survives every retag and rename. Two files sharing one are the same recording, whatever their tags say. |
| **Artist + title + duration** | For the (many) files carrying no ISRC. The title is folded and stripped of release noise — `(2011 Remaster)`, `(Album Version)` — the artist is reduced to the lead credit, and duration keeps a live take out of the studio cut's group. |

A file with no ISRC joins the group its name matches. Two files with
*different* ISRCs are never merged on names alone, and an untagged file
sitting between two disagreeing ISRCs joins neither — the answer that can
only ever cost a duplicate left behind, never a wrong file removed.

`--dedup-match isrc` restricts the scan to the first row (the safest, and
useless on a library that was never tagged); `--dedup-match tags` restricts
it to the second. `--dedup-tolerance SECONDS` (default 4) is how far two
durations may differ; `--dedup-keep-version-noise` treats a remaster as its
own recording.

### Confirming against the audio

`--dedup-verify` fingerprints each group and splits it where the audio
disagrees, so a group only survives if the sound agrees with the tags. This
is the expensive check applied cheaply: fingerprints are computed per group,
never library-wide. It needs the same `SpotiFLAC[dedup]` extra as the section
above; without it the scan says so in a note and reports unverified groups
rather than silently passing them.

A file that cannot be fingerprinted leaves its group rather than staying in
it. Everything downstream of a group is a decision to remove files, so
"unsure" means "keep them all".

### Which copy survives

Quality tier first, then sample rate, bit depth, bitrate and size; a tagged
copy outranks an untagged one; the path breaks the last tie so two runs over
an unchanged library never disagree. `keep_paths=` overrides it per group
from the Python API.

### Resolving

Two deliberate decisions, not one: the scan reports, `--dedup-apply` acts.

- **Quarantine (default).** Redundant copies move into `.spotiflac-duplicates`
  inside the library (`--dedup-trash DIR` to put it elsewhere), mirroring the
  library's layout, and a manifest records every move. The folder is dotted,
  so the next scan does not walk back into it and re-find what it just moved.
  `--dedup-restore MANIFEST` puts everything back.
- **Delete** (`--dedup-delete --dedup-apply`) unlinks instead. A manifest is
  still written, marked as not restorable.

Before touching a file the resolver re-checks it against the scan: anything
that moved, changed size or changed mtime is left alone and reported, as is
anything outside the scanned folder, and a hard link to the copy being kept.
If the file that was supposed to *survive* is gone, the whole group is
skipped.

### Cost, and the scan cache

The scan costs a header read and a tag read per file, cached per path against
the file's mtime and size — a 4,000-file library scans in about a second on
local disk and rescans in a third of that. The cache lives in
`~/.spotiflac/.cache/library-dedup/` (`--dedup-no-cache` to ignore it) and is
checkpointed as it goes: a scan that dies halfway costs the files since the
last checkpoint, not the whole walk.

### The scan as a database

`--dedup-db FILE.db` writes the whole scan to SQLite: **one row per file**,
duplicated or not, with the groups recorded on top of it. What comes out is
an index of the library that something else can read — another tool, another
machine, a query typed into `sqlite3` — not just a list of duplicates.

```bash
spotiflac --dedup-library ~/Music --dedup-db ~/library.db
```

| Table | What's in it |
| --- | --- |
| `files` | Every file: path, size, mtime, duration, tags, ISRC, codec, sample rate, bit depth, bitrate, tier — plus `group_id` and `role` (`keep` / `duplicate`, `NULL` for the majority that belong to no group). |
| `groups` | One row per duplicate group: key, what matched it, label, member count, reclaimable bytes. |
| `meta` | Schema version, generator, when it ran, the root, the totals. |
| `duplicates` | A view joining the two, so the obvious query is already written. |

```sql
-- the biggest wins first
SELECT label, reclaimable_bytes FROM duplicates
WHERE role = 'duplicate' GROUP BY group_id
ORDER BY reclaimable_bytes DESC LIMIT 20;

-- what is in the library at all
SELECT codec, count(*), sum(size) FROM files GROUP BY codec;

-- everything a retagging pass should look at
SELECT path FROM files WHERE isrc = '' AND artist <> '';
```

The database reads back, too: `--dedup-from-db FILE.db` skips the scan
entirely and resolves from what it records, so the walk and the removal need
not happen in the same process, on the same machine, or on the same day —
a NAS can index overnight and a laptop can act on the result.

```bash
spotiflac --dedup-library /volume1/music --dedup-db /volume1/music-index.db   # on the NAS
spotiflac --dedup-from-db ~/music-index.db --dedup-apply                      # anywhere
```

Size and mtime travel in the database, and the resolver re-checks both before
touching anything — so an index that has gone stale skips files and says so
rather than removing the wrong ones.

The export costs nothing worth measuring next to the walk that produced it: a
4,000-file library writes a 1.9 MB database in about a hundredth of a second,
and reads back in the same. It is written to a `.partial` file and renamed
into place, so an interrupted export leaves the previous database intact
instead of a half-written one that still opens.

### In the GUI / web UI

The **Fix Local Files** tab has a **Library Duplicates** panel under the
fingerprint scanner, working on the same folder path as everything else in
that tab: pick the match mode, the duration slack, whether to confirm against
the audio and whether to write the `.db`, then **Scan Library**.

Each group comes back with a radio (which copy to keep) and a checkbox per
redundant copy (which ones to act on) — every duplicate ticked by default,
the kept copy never selectable. Change the radio and the copy it replaces
becomes selectable in its place. Then **Move to quarantine** (undoable, and
an **Undo** button appears with the manifest) or **Delete permanently**,
both behind a confirmation.

A library can produce thousands of groups; the panel is handed the first 500
and says how many it is not showing — resolve those, scan again for the rest.
Anything the resolver left alone is reported individually, so a file that
changed since the scan is visible rather than silently missing from the count.

Backed by `scan_library_duplicates()` (background thread; results arrive on
the `app_library_dedup_results` push event, progress on
`app_library_dedup_progress`, failures on `app_library_dedup_error`),
`resolve_library_duplicates()` and `restore_library_duplicates()`. The report
stays on the backend between the scan and the resolve, so the browser sends
back only the paths it chose — and the resolver only ever accepts paths the
last scan actually reported as redundant.

### From Python

```python
from SpotiFLAC.core.library_dedup import (
    export_sqlite, load_report, resolve_duplicates, restore_manifest,
    scan_duplicates,
)

report = scan_duplicates("~/Music", verify=False)
print(report.summary())

for group in report.groups:
    print(group.label, "→ keep", group.keeper.path)

# Dry run first; nothing is touched until dry_run=False.
plan = resolve_duplicates(report)
print(plan.summary())

done = resolve_duplicates(report, dry_run=False)
restore_manifest(done.manifest_path)

# Or hand the scan to something else — and pick it up again later.
export_sqlite(report, "~/library.db")
resolve_duplicates(load_report("~/library.db"), dry_run=False)
```

`report.to_dict()` is the recap and every group as JSON, which
`spotiflac --dedup-library ~/Music --json` prints; `to_dict(include_files=True)`
adds every scanned file, though for a library of that size the `.db` is the
better shape for it.

### Options

| Flag | What it does |
| --- | --- |
| `--dedup-library PATH` | Folder to scan. |
| `--dedup-match` | `both` (default), `isrc`, `tags`. |
| `--dedup-tolerance SECONDS` | Duration slack within a group (default 4). |
| `--dedup-keep-version-noise` | A remaster is its own recording, not a duplicate. |
| `--dedup-verify` | Confirm each group against the audio. Needs the `dedup` extra. |
| `--dedup-threshold` | Fingerprint similarity for `--dedup-verify` (default 0.95). |
| `--dedup-apply` | Actually resolve. Without it the command only reports. |
| `--dedup-delete` | With `--dedup-apply`, unlink instead of quarantining. Not undoable. |
| `--dedup-trash DIR` | Where quarantined copies go. |
| `--dedup-limit N` | Resolve at most N files. |
| `--dedup-db FILE.db` | Also write the scan to a SQLite index. |
| `--dedup-from-db FILE.db` | Skip the scan; read the report back from one. |
| `--dedup-restore MANIFEST` | Undo a previous `--dedup-apply` run. |
| `--dedup-no-cache` | Re-read every file instead of reusing the scan cache. |
| `--no-recursive`, `--json`, `--verbose` | As everywhere else. |

The same thing runs standalone, for a machine that has the package but no
reason to start the GUI:

```bash
python -m SpotiFLAC.tools.library_dedup_cli ~/Music --verify --verbose
```

---


## MusicBrainz Enrichment

SpotiFLAC automatically queries MusicBrainz in the background (when an ISRC is available) while the audio is being downloaded, adding professional-grade tags at no extra time cost. Fields written when found:

| Tag | Description |
| --- | --- |
| `GENRE` | Genre |
| `ORGANIZATION` | Record label |
| `BPM` | Beats per minute |
| `UPC` | Release barcode / UPC |
| `ISRC` | Track ISRC code (normalized) |
| `ITUNESADVISORY` | Set to `1` when the release is marked explicit |

---


## Download Validation

After each download, SpotiFLAC validates the file to detect common issues:

- **Preview detection** — if the expected duration is ≥ 60 s but the downloaded file is ≤ 35 s, the file is deleted and the download is retried with the next extension.
- **Duration mismatch** — for tracks longer than 90 s, a deviation greater than 25% (or 15 s minimum) from the expected duration is treated as a corrupt download and the file is removed.

---
