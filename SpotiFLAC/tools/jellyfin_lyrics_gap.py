"""Re-space the word-by-word lyrics already embedded in a library.

SPOTIFLAC_LYRICS_JELLYFIN_COMPAT only reaches tracks downloaded after it was
switched on. Everything already on disk still carries a plain space between
timed words, which Jellyfin trims away on its next library scan — so this
walks a folder and rewrites those tags in place.

Nothing is re-fetched: the gap is swapped into the lyrics the file already
has, so a run is offline, fast, and leaves timings untouched.

    python -m SpotiFLAC.tools.jellyfin_lyrics_gap ~/Music/SpotiFLAC
    python -m SpotiFLAC.tools.jellyfin_lyrics_gap ~/Music/SpotiFLAC --apply

Without --apply it only reports what it would change.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.id3 import USLT

from SpotiFLAC.core.lyrics import JELLYFIN_WORD_GAP, apply_jellyfin_word_gap

#: Containers the downloader writes. Anything else is skipped in silence —
#: a music folder is full of covers, .nfo files and stray downloads.
AUDIO_SUFFIXES = {".flac", ".m4a", ".mp3", ".ogg", ".opus"}

#: The Vorbis-comment spellings seen in the wild; the first one present wins.
VORBIS_KEYS = ("LYRICS", "UNSYNCEDLYRICS", "SYNCEDLYRICS")


def _read_lyrics(audio) -> tuple[str, str] | None:
    """The file's lyrics as (tag key, text), or None when it carries none."""
    tags = audio.tags
    if not tags:
        return None

    if "\xa9lyr" in tags:  # MP4/M4A
        value = tags["\xa9lyr"]
        return "\xa9lyr", value[0] if isinstance(value, list) else str(value)

    for frame_id in tags.keys():  # ID3/MP3 — USLT carries a language suffix
        if str(frame_id).startswith("USLT"):
            return str(frame_id), tags[frame_id].text

    for key in VORBIS_KEYS:  # FLAC/Ogg/Opus
        if key in tags:
            value = tags[key]
            return key, value[0] if isinstance(value, list) else str(value)

    return None


def _write_lyrics(audio, key: str, text: str) -> None:
    if key.startswith("USLT"):
        # setall() replaces the whole USLT collection, so the rewritten frame
        # has to be handed back alongside its siblings: a file can carry one
        # per language, and passing only ours would delete the rest.
        rebuilt = [
            (
                USLT(encoding=3, lang=frame.lang, desc=frame.desc, text=text)
                if frame.HashKey == key
                else frame
            )
            for frame in audio.tags.getall("USLT")
        ]
        audio.tags.setall("USLT", rebuilt)
    elif key == "\xa9lyr":
        audio.tags["\xa9lyr"] = [text]
    else:
        audio.tags[key] = [text]
    audio.save()


def process(root: Path, apply: bool) -> tuple[int, int, int]:
    """Returns (files seen, files with word-by-word lyrics, files rewritten)."""
    seen = timed = rewritten = 0

    for path in sorted(root.rglob("*")):
        if path.suffix.lower() not in AUDIO_SUFFIXES:
            continue
        seen += 1

        try:
            audio = MutagenFile(path)
        except Exception as exc:  # a truncated or half-written download
            print(f"  ! unreadable: {path.name}: {exc}", file=sys.stderr)
            continue
        if audio is None:
            continue

        found = _read_lyrics(audio)
        if not found:
            continue
        key, text = found
        # No inline <mm:ss.xx> means line-synced lyrics, which Jellyfin's trim
        # cannot damage — there is only one cue per line.
        if "<" not in text:
            continue
        timed += 1

        gapped = apply_jellyfin_word_gap(text)
        if gapped == text:  # already carries the gap
            continue

        rewritten += 1
        if not apply:
            print(f"  would rewrite: {path.name}")
            continue

        try:
            _write_lyrics(audio, key, gapped)
            print(f"  rewritten: {path.name}")
        except Exception as exc:
            rewritten -= 1
            print(f"  ! write failed: {path.name}: {exc}", file=sys.stderr)

    return seen, timed, rewritten


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="folder to walk")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the files; without it the run only reports",
    )
    args = parser.parse_args()

    if not args.root.is_dir():
        print(f"not a folder: {args.root}", file=sys.stderr)
        return 2

    seen, timed, rewritten = process(args.root, args.apply)
    verb = "rewritten" if args.apply else "to rewrite"
    print(
        f"\n{seen} audio files, {timed} with word-by-word lyrics, "
        f"{rewritten} {verb} (gap U+{ord(JELLYFIN_WORD_GAP):04X})"
    )
    if rewritten and args.apply:
        print("Now rescan the library in Jellyfin so it regenerates its .lrc cache.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
