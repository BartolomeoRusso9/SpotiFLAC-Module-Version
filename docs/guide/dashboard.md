[← Back to the guide](README.md)

# Your library, in numbers

Every finished track has been written to a durable log for a while now, because
three features needed to look one track up: quotas count against it,
subscriptions check it before re-fetching a release, and "have I already got
this?" is answered from it rather than by trawling the filesystem.

Read as a whole rather than one row at a time, the same log answers a different
kind of question — the one people actually ask about their own library.

```bash
spotiflac --stats
```

```text
SpotiFLAC — all time

  1 284 track(s) · 317 artist(s) · 96 album(s) · 41.2 GB
  3d 14h of music

  Top artists
    Foo Fighters      ████████████████████████  74
    Queen             ██████████████████······  56
    …

  Top genres  (known for 812 of 1284)
    Rock              ████████████████████████  300
    …

  By decade
    1970s  ███████████·············  96
    …

  Busiest day: 2026-03-14 (61 tracks)
  Active on 214 day(s) · longest streak 19 day(s) · 4 day(s) running
```

In the GUI and in `--web` the same numbers are a screen of their own — the
**Stats** entry in the sidebar — with a period selector and the rankings drawn
as bars.

---

## What it shows

- **Totals** — tracks, distinct artists and albums, bytes on disk, total
  running time, and the failure rate if anything failed.
- **Top artists** — every *credited* artist, not just the first: a feature
  credits both names.
- **Top albums**, and **tracks fetched more than once** (a re-download after a
  quality upgrade, or the same song from two playlists — worth surfacing
  precisely because it is not obvious).
- **Top genres** and **decades** — what the music is, and when it came out.
- **Providers** and **formats** — which extension actually served your library,
  which is also the honest answer to "is this really all FLAC?".
- **A month-by-month timeline**, including the months nothing happened in.
- **Activity** — by weekday and hour *in your own timezone*, the busiest single
  day, how many days you downloaded on at all, and the longest run of
  consecutive days.

---

## What it cannot know, and says so

Genre, release year and duration are recorded from the moment the feature
landed. Rows written before that have none, and a genre is only recorded if
metadata enrichment was on when the track was downloaded (`--no-enrich` turns
it off).

So every section that depends on them reports its own coverage —
`known for 812 of 1284` above, `known`/`unknown` in the API. A dashboard that
overstates what it knows is worse than one that admits a gap. Nothing is
back-filled: the metadata that knew is gone by the time the file is on disk,
and re-reading a hundred thousand files is not an answer.

One consequence worth knowing: downloads made from the **GUI or `--web`** are
recorded too, from the same release this dashboard arrived in. Before that only
CLI runs were, which also means per-account quotas were not counting anything a
web user downloaded.

---

## Periods and accounts

| Flag | Period |
| --- | --- |
| `--stats` | Everything. |
| `--stats --stats-year 2026` | One calendar year, in local time. |
| `--stats --stats-days 30` | The last 30 days. |
| `--stats --stats-top 20` | Longer rankings (default 10). |
| `--stats --json` | The same document, for a script. |
| `--stats --stats-user ada` | One account's history (`--web-multiuser`). |

In multi-user mode the GUI and the REST API always scope the dashboard to the
account asking: one person's numbers are not a view of everybody's downloads.
`--stats-user` is the operator's way to look at one account from the machine
itself; without it the CLI reports the whole instance.

---

## Elsewhere

- **REST** — `GET /api/v1/stats?year=&days=&top=`, see the
  [REST API](rest-api.md).
- **Python** —

  ```python
  from SpotiFLAC.core import stats

  document = stats.wrapped(window=stats.year_window(2026), top=20)
  print(document["totals"], document["top_artists"])
  print(stats.format_wrapped(document))   # the text rendering above
  ```

The store itself is the SQLite database at `~/.spotiflac/spotiflac.db`
(`SPOTIFLAC_DB_PATH` moves it), so anything not covered here is a query away.
