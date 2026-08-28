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
