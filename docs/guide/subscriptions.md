# Following Artists, and Upgrading a Library

Two passes over things you already own or already follow. Neither downloads
anything you have not asked it to.

---

## Following an artist

`--watch` re-syncs one URL on a timer, which is right for a playlist you keep
pointed at. It is the wrong tool for an artist: it re-resolves the whole back
catalogue every pass and nothing remembers what it has already seen, so it can
never answer the question you actually have — *what came out since last time?*

A **subscription** is a followed URL plus the set of releases already seen.

```bash
spotiflac --subscribe "https://open.spotify.com/artist/<id>"
spotiflac --subscriptions          # list
spotiflac --check-subscriptions    # what's new? (reports only)
spotiflac --check-subscriptions --download --profile my-setup
spotiflac --unsubscribe "https://open.spotify.com/artist/<id>"
```

### The first check does not download your back catalogue

This is the one behaviour worth knowing before you use it. A new subscription
records everything that exists **today** as already-seen and reports nothing.
The first thing it ever fetches is the first thing released *after* you
subscribed.

That is what "follow an artist" means to most people, and the alternative —
typing one command and being handed four hundred tracks — is a bad surprise to
inflict by default. If you *do* want the existing catalogue:

```bash
# On a new subscription:
spotiflac --check-subscriptions --subscribe-backfill --download

# On one you already follow — forget what it has seen first:
spotiflac --subscribe-reset "https://open.spotify.com/artist/<id>"
spotiflac --check-subscriptions --subscribe-backfill --download
```

### Options

| Flag | What it does |
| --- | --- |
| `--subscribe URL` | Follow. Re-running with the same URL updates rather than duplicating. |
| `--subscribe-name NAME` | A label for listings; otherwise the artist name is filled in on the first check. |
| `--subscribe-groups` | `album`, `single`, `compilation`, `appears_on`, comma-separated — or `all`. Default `album,single`. |
| `--subscribe-backfill` | Treat a first check's existing catalogue as new. |
| `--subscribe-reset URL` | Empty the seen-set, so everything counts as new again. |
| `--check-subscriptions` | Check every enabled subscription. |
| `--download` | …and fetch what is new. Without it, the check only reports. |
| `--output-dir` | Where new releases land. Falls back to the subscription's own folder, then the profile's. |
| `--json` | Machine-readable output, for cron. |

A subscription owns no download settings of its own: a fetched release lands
with exactly the naming, quality, lyrics and tagging a manual download would
have used, read from `--profile` or `config.json`.

### Running it on a schedule

```cron
0 9 * * * spotiflac --check-subscriptions --download --profile nas --json >> ~/spotiflac.log
```

Marking happens at check time, not after the download. A release whose
download fails is therefore not re-offered on every subsequent check forever —
the failure is reported by the run (and by `--json`), and `--subscribe-reset`
is how you ask for another attempt.

Checks are sequential and read only the discography *listing*, not each
album's tracks — one artist is a handful of requests, so twenty artists on an
hourly cron is reasonable.

### In the GUI / web UI

The **Following** panel in the sidebar does the same things: follow, pause,
reset, unfollow, and "Check for new" / "Check & download" as two separate
buttons.

---

## Upgrading a library

Finds the files in a folder that are below the quality you actually want, and
optionally re-downloads them.

```bash
# Report only — always start here.
spotiflac --upgrade-library ~/Music --upgrade-target LOSSLESS

# Include files that claim Hi-Res but whose audio stops at CD range.
spotiflac --upgrade-library ~/Music --upgrade-target HI_RES --upgrade-verify-hires

# Actually re-fetch them.
spotiflac --upgrade-library ~/Music --upgrade-download --profile my-setup
```

### Tiers

Three, deliberately coarse, because they are the three that change what you
hear:

| Tier | What counts |
| --- | --- |
| `lossy` | MP3, AAC, Opus, Vorbis, WMA — information is gone |
| `lossless` | FLAC / ALAC / WAV up to 16-bit / 48 kHz |
| `hires` | lossless above either of those limits |

A file is a candidate when its tier is below the target's. Sample rate and bit
depth come from the container header, so a scan costs a header read per file
rather than a decode — a large library scans in seconds.

The container alone does not decide: `.m4a` holds both AAC (lossy) and ALAC
(lossless), so the codec is what is actually checked.

### Fake Hi-Res

A file can *declare* 24/96 and contain nothing above 22 kHz — the fingerprint
of something upsampled from a CD. `--upgrade-verify-hires` runs the spectral
check from [`core/hires_check.py`](../../SpotiFLAC/core/hires_check.py) and
reclassifies such a file down to the tier its content justifies, which is the
only way "upgrade my library to Hi-Res" gives an honest answer.

It is off by default because it decodes ~30 seconds per file, and needs the
optional extra:

```bash
pip install 'SpotiFLAC[hires]'
```

Without it the scan says so once and carries on without reclassifying.

### How a candidate is matched back to Spotify

ISRC first — it identifies a *recording* and survives every retagging and
rename — then a title/artist search for files that have no ISRC, which are
exactly the badly-tagged ones this feature exists to improve. Files that
cannot be matched are listed rather than silently dropped.

### Options

| Flag | What it does |
| --- | --- |
| `--upgrade-library PATH` | Folder to scan. |
| `--upgrade-target` | `LOSSLESS` (default), `HI_RES`, … — anything `normalize_quality()` accepts. |
| `--upgrade-verify-hires` | Also flag fake Hi-Res. Slow; needs the `hires` extra. |
| `--upgrade-download` | Re-fetch what was found. Without it the command only reports. |
| `--upgrade-limit N` | Stop after N files — useful for a first trial run. |
| `--no-recursive` | Do not descend into subfolders. |
| `--json` | Machine-readable output. |

The default is a dry run, deliberately: an upgrade re-downloads files, which
is not something a command should do because you were curious what it would
find.
