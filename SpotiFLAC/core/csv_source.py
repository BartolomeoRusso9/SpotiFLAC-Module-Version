"""core/csv_source.py — a CSV file as an input source.

Every existing entry point takes a *link*: a track, an album, a playlist, an
artist. That covers what a streaming service can hand over, and misses what
people actually have lying around — the export of a playlist they no longer
have access to, a spreadsheet of records to buy, the CSV a DJ tool or a
"transfer my library" service produced. Those are lists of *tracks*, usually
without a single usable URL in them.

This module turns such a file into something the downloader already knows how
to consume: a list of URLs.

Two halves, deliberately separate
---------------------------------
`read_rows()` is pure parsing — no network, no Spotify, no downloader. It
sniffs the delimiter, works out which columns mean what (by header name, or
by looking at the values when there is no header) and yields `CsvRow`s. It is
the part with all the awkward real-world cases in it, and the part worth
testing exhaustively.

`resolve_rows()` is the network half: rows that already carry a link keep it,
rows that carry only text are matched against Spotify's catalogue and scored,
and anything that doesn't match well enough is *reported* rather than guessed
at. A wrong match here means a file on disk with the right name and the wrong
music in it, which is worse than a row the user is told about and can fix.

Supported shapes
----------------
Anything with a header naming the usual columns works, which covers the
common exporters:

    Exportify / Spotify playlist exports
        "Track URI","Track Name","Artist Name(s)","Album Name","ISRC","Duration (ms)"
    Soundiiz / TuneMyMusic style
        "Title","Artist","Album","ISRC"
    a bare list of links
        one URL per line, with or without a header

The column names are matched case-insensitively and ignoring punctuation, so
"Track Name", "track_name" and "TRACKNAME" are the same column.
"""

from __future__ import annotations

import asyncio
import csv
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .errors import ErrorKind, SpotiflacError
from .isrc_utils import normalize_isrc
from .text_match import fold, ratio, score_track_match, strip_noise

logger = logging.getLogger(__name__)

#: Tried in order when the delimiter isn't given and `csv.Sniffer` gives up.
DELIMITERS: tuple[str, ...] = (",", ";", "\t", "|")

#: How close a search result has to be before it is downloaded as "the track
#: this row meant". Tuned on the exports listed above: comfortably above what
#: a different song by the same artist scores, comfortably below what a
#: title with a "- Remastered 2011" suffix or a missing "(feat. …)" scores.
DEFAULT_MIN_SCORE = 0.62

#: Rows resolved in parallel. Every one of them is a Spotify query, so this
#: is a politeness limit rather than a performance one — the downloads
#: themselves are governed by `--max-concurrent`.
DEFAULT_CONCURRENCY = 4

#: Candidates asked for per row. More than this only adds noise: if the right
#: track isn't in the first handful, the query was wrong, not too short.
SEARCH_LIMIT = 8

_SPOTIFY_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")
_SPOTIFY_URI_RE = re.compile(
    r"^spotify:(track|album|playlist|artist):([A-Za-z0-9]{22})$"
)
_ISRC_LOOSE_RE = re.compile(r"^[A-Za-z]{2}[A-Za-z0-9]{3}\d{7}$")
_HEADER_NOISE_RE = re.compile(r"[^a-z0-9]+")

#: field → header aliases, most specific first. The order matters: a file
#: with both "Artist Name(s)" and "Album Artist Name(s)" must map `artist` to
#: the first, not to whichever the reader happens to see first.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "url": (
        "trackuri",
        "trackurl",
        "spotifyuri",
        "spotifyurl",
        "spotifytrackuri",
        "spotifytrackurl",
        "url",
        "uri",
        "link",
        "externalurl",
        "songurl",
        "trackid",
        "spotifyid",
        "permalink",
        "href",
    ),
    "isrc": ("isrc", "isrccode", "trackisrc"),
    "title": (
        "trackname",
        "title",
        "tracktitle",
        "songname",
        "song",
        "track",
        "titolo",
        "brano",
        "canzone",
        "titre",
        "name",
    ),
    "artist": (
        "artistnames",
        "artistname",
        "artists",
        "artist",
        "performer",
        "interprete",
        "artista",
        "albumartistnames",
        "albumartistname",
        "albumartist",
    ),
    "album": ("albumname", "albumtitle", "album", "release"),
    "duration": (
        "trackdurationms",
        "durationms",
        "trackduration",
        "duration",
        "lengthms",
        "length",
    ),
}


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CsvRow:
    """One line of the file, in the only terms this module cares about."""

    line: int
    url: str = ""
    isrc: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""
    duration_ms: int = 0

    @property
    def query(self) -> str:
        """What to search the catalogue for, when there is no link."""
        return " ".join(part for part in (self.title, self.artist) if part).strip()

    @property
    def label(self) -> str:
        """How this row is named in a report the user reads."""
        if self.title and self.artist:
            return f"{self.title} — {self.artist}"
        return self.title or self.url or self.isrc or f"line {self.line}"

    def to_dict(self) -> dict:
        return {
            "line": self.line,
            "url": self.url,
            "isrc": self.isrc,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True)
class CsvDocument:
    """A parsed file: what was found, and how it was read."""

    path: str
    delimiter: str
    has_header: bool
    columns: dict[str, str]
    rows: tuple[CsvRow, ...]
    #: Lines that carried nothing usable (blank, or only values this module
    #: has no meaning for). Reported so "300 lines in, 280 tracks out" can be
    #: explained rather than silently accepted.
    ignored_lines: tuple[int, ...] = ()

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "delimiter": self.delimiter,
            "has_header": self.has_header,
            "columns": dict(self.columns),
            "rows": len(self.rows),
            "ignored_lines": list(self.ignored_lines),
        }


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _normalize_header(value: str) -> str:
    return _HEADER_NOISE_RE.sub("", (value or "").strip().lower())


def _sniff_delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters="".join(DELIMITERS)).delimiter
    except csv.Error:
        # Sniffer refuses single-column files and files whose first lines are
        # inconsistent; counting is enough to tell "a;b;c" from "a,b,c", and
        # a file with none of them is a one-column list of links.
        first_line = sample.splitlines()[0] if sample.splitlines() else ""
        counts = {d: first_line.count(d) for d in DELIMITERS}
        best = max(counts, key=lambda d: counts[d])
        return best if counts[best] else ","


def _read_text(path: str | Path) -> str:
    file_path = Path(path)
    try:
        raw = file_path.read_bytes()
    except OSError as exc:
        raise SpotiflacError(
            ErrorKind.FILE_IO, f"Could not read {file_path}: {exc}"
        ) from exc
    for encoding in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    # latin-1 decodes any byte, so this is unreachable in practice; kept so a
    # future change to the list above can't silently return None.
    return raw.decode("utf-8", errors="replace")


def _map_columns(header: Sequence[str]) -> dict[str, str]:
    """field → the header that provides it, for the headers we recognise."""
    normalized = {_normalize_header(name): name for name in header if name}
    columns: dict[str, str] = {}
    used: set[str] = set()
    for field, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            name = normalized.get(alias)
            if name is not None and name not in used:
                columns[field] = name
                used.add(name)
                break
    return columns


def _looks_like_link(value: str) -> bool:
    value = (value or "").strip()
    return value.startswith(("http://", "https://", "spotify:"))


def _looks_like_isrc(value: str) -> bool:
    return bool(_ISRC_LOOSE_RE.match((value or "").strip().replace("-", "")))


def _to_int(value: str) -> int:
    """Duration column as milliseconds, accepting "3:41" and "221" too."""
    text = (value or "").strip()
    if not text:
        return 0
    if ":" in text:
        parts = text.split(":")
        try:
            numbers = [int(float(p)) for p in parts]
        except ValueError:
            return 0
        seconds = 0
        for number in numbers:
            seconds = seconds * 60 + number
        return seconds * 1000
    try:
        number = int(float(text))
    except ValueError:
        return 0
    # A track is never 400 milliseconds and rarely 400 minutes: a value this
    # small is seconds, which is what several exporters write.
    return number if number > 10000 else number * 1000


def normalize_link(value: str) -> str:
    """A link column value as a URL the downloader accepts, or "".

    Covers the three ways a Spotify track is written in an export: the open
    URL, the `spotify:track:…` URI (Exportify's "Track URI") and — in files
    whose link column is called "Track ID" — the bare id.
    """
    text = (value or "").strip()
    if not text:
        return ""

    uri_match = _SPOTIFY_URI_RE.match(text)
    if uri_match:
        kind, identifier = uri_match.groups()
        return f"https://open.spotify.com/{kind}/{identifier}"

    if text.startswith(("http://", "https://")):
        return text

    if _SPOTIFY_ID_RE.match(text):
        return f"https://open.spotify.com/track/{text}"

    return ""


def _row_from_mapping(line: int, values: dict[str, str]) -> CsvRow:
    return CsvRow(
        line=line,
        url=normalize_link(values.get("url", "")),
        isrc=normalize_isrc(values.get("isrc", "")),
        title=(values.get("title") or "").strip(),
        artist=(values.get("artist") or "").strip(),
        album=(values.get("album") or "").strip(),
        duration_ms=_to_int(values.get("duration", "")),
    )


def _row_from_positions(line: int, cells: Sequence[str]) -> CsvRow:
    """A row from a file with no header, read by what the values look like.

    A link is a link and an ISRC is an ISRC wherever they sit; whatever text
    is left over is read as title, then artist, then album — the order every
    headerless export this has been pointed at uses. A single text cell
    containing " - " is the other common shape ("Artist - Title"), and is
    split rather than searched for verbatim.
    """
    url = isrc = ""
    text_cells: list[str] = []
    for cell in cells:
        value = (cell or "").strip()
        if not value:
            continue
        if not url and _looks_like_link(value):
            url = normalize_link(value)
            continue
        if not isrc and _looks_like_isrc(value):
            isrc = normalize_isrc(value)
            continue
        text_cells.append(value)

    title = artist = album = ""
    if len(text_cells) == 1 and " - " in text_cells[0]:
        # "Artist - Title" is the near-universal convention for this shape,
        # and getting it backwards still searches for both words — the
        # scorer compares the pair, not one field at a time.
        artist, title = (part.strip() for part in text_cells[0].split(" - ", 1))
    else:
        if text_cells:
            title = text_cells[0]
        if len(text_cells) > 1:
            artist = text_cells[1]
        if len(text_cells) > 2:
            album = text_cells[2]

    return CsvRow(
        line=line, url=url, isrc=isrc, title=title, artist=artist, album=album
    )


def read_rows(path: str | Path, *, delimiter: str | None = None) -> CsvDocument:
    """Parses `path` into rows. Talks to nothing and needs no network.

    Raises `SpotiflacError` when the file cannot be read or holds no row this
    module can do anything with — an empty result is a mistake worth stopping
    for, not a run that downloads nothing and says it succeeded.
    """
    return read_text(_read_text(path), name=str(path), delimiter=delimiter)


def read_text(
    text: str,
    *,
    name: str = "",
    delimiter: str | None = None,
) -> CsvDocument:
    """Same as `read_rows`, for a file that never touches this filesystem.

    The GUI and the REST API are handed a CSV's *contents* — read in the
    browser, or posted in a request body — rather than a path they could
    open. Everything else about the parse is identical, and `name` is only
    used to name the file in errors and in the playlist written next to the
    tracks.
    """
    label = name or "(csv)"
    if not text.strip():
        raise SpotiflacError(ErrorKind.PARSE_ERROR, f"{label} is empty.")

    sample = "\n".join(text.splitlines()[:20])
    used_delimiter = delimiter or _sniff_delimiter(sample)

    reader = csv.reader(text.splitlines(), delimiter=used_delimiter)
    records = [record for record in reader]
    if not records:
        raise SpotiflacError(ErrorKind.PARSE_ERROR, f"{label} holds no rows.")

    columns = _map_columns(records[0])
    # A first line that names at least one column we understand is a header.
    # One that doesn't is data: a bare list of links has no header at all,
    # and reading it as one would silently drop its first track.
    has_header = bool(columns)
    field_index: dict[str, int] = {}
    if has_header:
        header = records[0]
        # `column`, not `name`: this used to bind the loop variable to the
        # function's `name` parameter, so after a CSV *with* a header the
        # document's path came out as whichever column matched last —
        # "Duration (ms)" instead of "playlist.csv". That name is what the
        # Logs line, the album card's title and the written playlist all
        # show, so the mix-up was visible in three places.
        for field, column in columns.items():
            field_index[field] = header.index(column)

    rows: list[CsvRow] = []
    ignored: list[int] = []
    for offset, cells in enumerate(records[1:] if has_header else records, start=1):
        line = offset + 1 if has_header else offset
        if not any((cell or "").strip() for cell in cells):
            continue
        if has_header:
            values = {
                field: cells[index] if index < len(cells) else ""
                for field, index in field_index.items()
            }
            row = _row_from_mapping(line, values)
        else:
            row = _row_from_positions(line, cells)

        if row.url or row.isrc or row.title:
            rows.append(row)
        else:
            ignored.append(line)

    if not rows:
        raise SpotiflacError(
            ErrorKind.PARSE_ERROR,
            f"{label}: no track found. Expected a link column, an ISRC column, "
            "or a title (with an artist alongside it).",
        )

    return CsvDocument(
        path=name,
        delimiter=used_delimiter,
        has_header=has_header,
        columns=columns,
        rows=tuple(rows),
        ignored_lines=tuple(ignored),
    )


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


# The scoring primitives moved to core/text_match.py so the local
# auto-tagger scores a candidate the same way a CSV row does — it used to
# carry its own, weaker version. Re-exported under the old private names
# because they are used further down this module.
_fold = fold
_strip_noise = strip_noise
_ratio = ratio


def match_score(row: CsvRow, candidate: Any) -> float:
    """How well a search result answers a row, in 0…1.

    Thin wrapper over text_match.score_track_match() — see there for how the
    fields are weighted and why.
    """
    return score_track_match(
        title=row.title,
        artist=row.artist,
        album=row.album,
        duration_ms=row.duration_ms,
        candidate=candidate,
    )


def best_match(row: CsvRow, candidates: Iterable[Any]) -> tuple[Any | None, float]:
    best: Any | None = None
    best_score = 0.0
    for candidate in candidates:
        score = match_score(row, candidate)
        if score > best_score:
            best, best_score = candidate, score
    return best, best_score


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedRow:
    row: CsvRow
    url: str
    #: "link" (the row carried one), "search" or "isrc" — worth keeping so a
    #: report can distinguish "you gave me this" from "I found this for you".
    how: str
    score: float = 1.0
    matched: str = ""

    def to_dict(self) -> dict:
        return {
            "line": self.row.line,
            "input": self.row.label,
            "url": self.url,
            "how": self.how,
            "score": self.score,
            "matched": self.matched,
        }


@dataclass(frozen=True)
class UnresolvedRow:
    row: CsvRow
    reason: str
    #: Best candidate seen and its score, when the problem was "nothing was
    #: close enough" rather than "nothing came back". The user fixing the
    #: file wants to know which of the two it was.
    best: str = ""
    score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "line": self.row.line,
            "input": self.row.label,
            "reason": self.reason,
            "best": self.best,
            "score": self.score,
        }


@dataclass(frozen=True)
class CsvResolution:
    document: CsvDocument
    resolved: tuple[ResolvedRow, ...]
    unresolved: tuple[UnresolvedRow, ...]

    @property
    def urls(self) -> list[str]:
        """Every resolved link, in file order and without repeats.

        A CSV that lists the same track twice must not download it twice;
        the merged-playlist planner dedups by recording as well, but doing
        it here also halves the metadata fetches for such a file.
        """
        seen: set[str] = set()
        ordered: list[str] = []
        for entry in self.resolved:
            if entry.url in seen:
                continue
            seen.add(entry.url)
            ordered.append(entry.url)
        return ordered

    def to_dict(self) -> dict:
        return {
            "file": self.document.to_dict(),
            "resolved": [entry.to_dict() for entry in self.resolved],
            "unresolved": [entry.to_dict() for entry in self.unresolved],
            "urls": self.urls,
        }


async def _search(client: Any, query: str, limit: int = SEARCH_LIMIT) -> list:
    try:
        return await client.search_tracks_async(query, limit=limit)
    except Exception as exc:
        logger.debug("[csv] search failed for %r: %s", query, exc)
        return []


async def _spotify_url_for_isrc(isrc: str, resolver: Any | None) -> str:
    """The Spotify link for an ISRC, via the cross-platform resolver.

    Used only for rows that carry an ISRC and nothing else to search with.
    Unlike a text search this is an identity lookup: either the ISRC is known
    and the answer is the right recording, or it isn't and the row is
    reported.
    """
    if resolver is None:
        from .link_resolver import LinkResolver

        resolver = LinkResolver()
    try:
        return await resolver.spotify_url_for_isrc_async(isrc)
    except Exception as exc:
        logger.debug("[csv] ISRC lookup failed for %s: %s", isrc, exc)
        return ""


async def _resolve_one(
    row: CsvRow,
    *,
    client: Any,
    resolver: Any | None,
    min_score: float,
) -> ResolvedRow | UnresolvedRow:
    if row.url:
        return ResolvedRow(row=row, url=row.url, how="link")

    if row.query:
        candidates = await _search(client, row.query)
        candidate, score = best_match(row, candidates)
        if candidate is not None and score >= min_score:
            return ResolvedRow(
                row=row,
                url=getattr(candidate, "external_url", "")
                or f"https://open.spotify.com/track/{candidate.id}",
                how="search",
                score=score,
                matched=f"{candidate.title} — {candidate.artists}",
            )
        # An ISRC alongside the text is a second, exact chance before giving
        # up: exports whose titles are localised or truncated still carry it.
        if row.isrc:
            url = await _spotify_url_for_isrc(row.isrc, resolver)
            if url:
                return ResolvedRow(row=row, url=url, how="isrc", score=1.0)
        if candidate is None:
            return UnresolvedRow(row=row, reason="nothing found")
        return UnresolvedRow(
            row=row,
            reason=f"no match above {min_score:g}",
            best=f"{candidate.title} — {candidate.artists}",
            score=score,
        )

    if row.isrc:
        url = await _spotify_url_for_isrc(row.isrc, resolver)
        if url:
            return ResolvedRow(row=row, url=url, how="isrc", score=1.0)
        return UnresolvedRow(row=row, reason="ISRC not found")

    return UnresolvedRow(row=row, reason="no link, ISRC or title")


async def resolve_rows(
    rows: Sequence[CsvRow],
    *,
    document: CsvDocument | None = None,
    client: Any | None = None,
    resolver: Any | None = None,
    min_score: float = DEFAULT_MIN_SCORE,
    concurrency: int = DEFAULT_CONCURRENCY,
    on_progress: Callable[[int, int], None] | None = None,
) -> CsvResolution:
    """Turns rows into links, searching the catalogue for the ones that need it.

    `client` is anything with `search_tracks_async(query, limit=…)` — the
    Spotify metadata client in production, a fake in tests. It is only built
    when a row actually needs a search, so a CSV of links resolves without
    touching the network at all.
    """
    if document is None:
        document = CsvDocument(
            path="",
            delimiter=",",
            has_header=False,
            columns={},
            rows=tuple(rows),
        )

    needs_search = any(not row.url for row in rows)
    if needs_search and client is None:
        from .spotify_metadata import SpotifyMetadataClient

        client = SpotifyMetadataClient()

    semaphore = asyncio.Semaphore(max(1, concurrency))
    done = 0
    total = len(rows)

    async def _worker(row: CsvRow):
        nonlocal done
        async with semaphore:
            try:
                outcome = await _resolve_one(
                    row, client=client, resolver=resolver, min_score=min_score
                )
            except Exception as exc:
                # One unreadable row must not take the file down with it.
                logger.debug("[csv] line %d failed: %s", row.line, exc)
                outcome = UnresolvedRow(row=row, reason=str(exc) or "lookup failed")
            done += 1
            if on_progress is not None:
                on_progress(done, total)
            return outcome

    outcomes = await asyncio.gather(*(_worker(row) for row in rows))

    resolved = tuple(o for o in outcomes if isinstance(o, ResolvedRow))
    unresolved = tuple(o for o in outcomes if isinstance(o, UnresolvedRow))
    return CsvResolution(document=document, resolved=resolved, unresolved=unresolved)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def write_unresolved(path: str | Path, unresolved: Sequence[UnresolvedRow]) -> int:
    """Writes the rows that didn't match to a CSV of the same shape.

    The point is that the file can be edited (a corrected title, a pasted
    link) and fed straight back to `--csv`, rather than the user having to
    reconstruct which of 300 lines were missed.
    """
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                ["line", "title", "artist", "album", "isrc", "url", "reason", "closest"]
            )
            for entry in unresolved:
                row = entry.row
                writer.writerow(
                    [
                        row.line,
                        row.title,
                        row.artist,
                        row.album,
                        row.isrc,
                        row.url,
                        entry.reason,
                        entry.best,
                    ]
                )
    except OSError as exc:
        raise SpotiflacError(
            ErrorKind.FILE_IO, f"Could not write {target}: {exc}"
        ) from exc
    return len(unresolved)


def format_resolution(resolution: CsvResolution, *, verbose: bool = False) -> str:
    """Human-readable rendering, for `--csv-dry-run` and the run header."""
    document = resolution.document
    lines = [
        f"CSV: {document.path or '(rows)'}",
        f"  delimiter {document.delimiter!r} · "
        f"{'header: ' + ', '.join(f'{k}={v}' for k, v in document.columns.items()) if document.columns else 'no header (values read by shape)'}",
        f"  {len(document.rows)} row(s) · {len(resolution.resolved)} resolved · "
        f"{len(resolution.unresolved)} unresolved",
    ]
    if document.ignored_lines:
        lines.append(f"  ignored line(s): {len(document.ignored_lines)}")

    if verbose and resolution.resolved:
        lines.append("")
        for entry in resolution.resolved:
            suffix = (
                f"  → {entry.matched} ({entry.score:.2f})"
                if entry.how == "search"
                else f"  → {entry.how}"
            )
            lines.append(f"  ✓ line {entry.row.line}: {entry.row.label}{suffix}")

    if resolution.unresolved:
        lines.append("")
        lines.append("  Not matched:")
        for missed in resolution.unresolved:
            closest = (
                f" (closest: {missed.best} {missed.score:.2f})" if missed.best else ""
            )
            lines.append(
                f"  ✗ line {missed.row.line}: {missed.row.label} — "
                f"{missed.reason}{closest}"
            )

    return "\n".join(lines)
