[← Back to the guide](README.md)

# Downloading from a CSV

Every other way in takes a *link*: a track, an album, a playlist, an artist.
That covers what a streaming service can hand over, and misses what people
actually have lying around — the export of a playlist you no longer have
access to, a spreadsheet of records to find, the file a "transfer my library"
service produced, a list of songs a friend sent you. Those are lists of
**tracks**, usually without a single usable URL in them.

`--csv` takes one of those files and downloads what it names.

```bash
spotiflac --csv wishlist.csv ~/Music
```

The whole file is treated as one playlist: a track listed twice is downloaded
once, tracks already sitting in the destination are never fetched again, and
an M3U named after the file is written next to them (`--m3u none` turns that
off). Running the same command again after adding rows only downloads the new
ones.

---

## What the file can look like

The delimiter is detected (`,`, `;`, tab, `|`) and the columns are matched by
name, case- and punctuation-insensitively — so `Track Name`, `track_name` and
`TRACKNAME` are the same column. These all work without configuration:

**An Exportify / Spotify playlist export**

```csv
"Track URI","Track Name","Artist Name(s)","Album Name","ISRC","Duration (ms)"
"spotify:track:4uLU6hMCjMI75M1A2tKUQC","Never Gonna Give You Up","Rick Astley","Whenever You Need Somebody","GBARL9300135","213573"
```

**A Soundiiz / TuneMyMusic style list, in any language**

```csv
Titolo;Artista;Album
Everlong;Foo Fighters;The Colour and the Shape
```

**A bare list of links, with or without a header**

```text
https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC
spotify:track:1301WleyT98MSxVHPZCA6M
https://open.spotify.com/album/6QaVfG1pHYl1z15ZxkvVDW
```

An album or playlist link in a row is expanded, so a CSV can mix "this one
track" and "all of this album". Links to any service the downloader already
accepts work, not just Spotify.

Recognised columns:

| Meaning | Column names it answers to |
| --- | --- |
| link | `Track URI`, `Track URL`, `Spotify URI`, `URL`, `URI`, `Link`, `Track ID`, … |
| ISRC | `ISRC` |
| title | `Track Name`, `Title`, `Song`, `Titolo`, `Brano`, `Titre`, `Name`, … |
| artist | `Artist Name(s)`, `Artist`, `Artists`, `Performer`, `Artista`, … |
| album | `Album Name`, `Album`, `Release` |
| duration | `Duration (ms)`, `Track Duration`, `Length` — accepts `221`, `221000` or `3:41` |

A file with no header at all is read by what its values look like: a link is a
link and an ISRC is an ISRC wherever they sit, and the leftover text is read
as title, then artist, then album. A single free-text column containing
`Artist - Title` is split on the dash.

---

## Rows without a link

A row that carries only a title (and ideally an artist) is searched for in the
catalogue and the results are scored: title first, artist second, album and
duration only as tie-breakers. A running time that is out by more than seven
seconds actively pushes a candidate down — same name, different recording, and
downloading an eleven-minute DJ mix instead of the single is exactly the
mistake worth avoiding.

Anything that does not score at least `--csv-min-score` (**0.62** by default)
is **reported, not guessed at**. A wrong match is a file on disk with the
right name and the wrong music in it, which is worse than a row you were told
about and can fix.

A row that also carries an ISRC gets a second, exact chance before being given
up on — useful for exports whose titles are localised or truncated.

### See what it would do first

```bash
spotiflac --csv wishlist.csv --csv-dry-run
```

```text
CSV: wishlist.csv
  delimiter ';' · header: title=Titolo, artist=Artista, album=Album
  120 row(s) · 118 resolved · 2 unresolved

  ✓ line 2: Everlong — Foo Fighters  → Everlong — Foo Fighters (1.00)
  …
  Not matched:
  ✗ line 44: Sconosciuto — Nessuno — no match above 0.62 (closest: … 0.31)
```

Nothing is downloaded, no destination is needed, and `--json` gives the same
thing as a document.

### Fix the ones that missed

```bash
spotiflac --csv wishlist.csv ~/Music --csv-unresolved missed.csv
```

`missed.csv` is itself a valid `--csv` input: correct a title, paste a link,
and feed it back in. It is written even if the download that follows is
interrupted — the rows that need your attention are the part you can act on.

---

## Every flag

| Flag | What it does |
| --- | --- |
| `--csv FILE` | Download every track the file lists. |
| `--csv-dry-run` | Resolve and report; download nothing. |
| `--csv-min-score 0..1` | How close a catalogue match must be (default `0.62`). |
| `--csv-unresolved FILE` | Write the unmatched rows to a CSV you can correct. |
| `--csv-concurrency N` | Rows looked up at a time (default `4`). Unrelated to `--max-concurrent`. |
| `--csv-delimiter CHAR` | Override the automatic detection. |

Everything else about the run is unchanged: `--service`, `--quality`,
`--filename-format`, `--transcode-to`, `--profile`, notifications, hooks and
`--json` all behave exactly as they do for a link.

`--watch MINUTES` works and is worth knowing about: it re-reads the file on
every cycle, so a CSV some other tool keeps up to date becomes a folder that
keeps itself up to date. `--loop` does not apply — run the command again to
retry what failed.

---

## From the interface

**GUI and `--web`** — the file icon next to the link/search toggle on the home
screen. Pick a file and its tracks fill the ordinary track table, where you
select and download them like anything else. Rows that could not be matched
are named in the log.

The file is read *in the browser* and only its text is sent to Python, so this
works the same in the desktop window and over `--web` — a server never needs
to be able to see your disk.

**The terminal UI** (`--tui`) has a *CSV track list* field next to the URL
one, and a **Browse for a track list…** button that scans the usual folders
and previews a file before accepting it. A path there takes precedence over
the link. See the [Terminal UI](tui.md#a-csv-instead-of-a-url) page.

**REST** — `POST /api/v1/csv/resolve` with the file's contents; see the
[REST API](rest-api.md) page.

---

## Python

```python
from SpotiFLAC.core import csv_source

document = csv_source.read_rows("wishlist.csv")          # no network
resolution = await csv_source.resolve_rows(document.rows, document=document)

print(resolution.urls)                                   # ready to download
for missed in resolution.unresolved:
    print(missed.row.line, missed.row.label, missed.reason)
```

Or the whole thing at once, through the downloader:

```python
from SpotiFLAC.downloader import DownloadOptions, SpotiflacDownloader

downloader = SpotiflacDownloader(DownloadOptions(output_dir="~/Music"))
summary = await downloader.run_csv_async("wishlist.csv")
```
