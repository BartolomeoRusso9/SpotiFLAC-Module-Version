# Terminal UI (`--tui`)

The guided mode. One screen instead of fifteen questions, and the download
queue live in front of you while it runs.

```bash
spotiflac --tui
```

*(Or `python launcher.py --tui` if running from source.)*

`--interactive` still works and opens this same screen, with a warning that
it is deprecated. The wizard it used to open is gone. See
[Coming from the wizard](#coming-from-the-wizard) below.

---

## What is on the screen

A sidebar on the left picks the panel; everything else is that panel.

| Panel | What it is for |
| --- | --- |
| **Download** | Every setting, all editable, in whatever order you like |
| **Search** | Find something in the catalogue and make it the URL |
| **Tracks** | What is behind the link, and which of it you want |
| **Queue** | The live run: one bar per track, plus the totals |
| **Session** | Recent URLs and saved profiles |
| **Extensions** | The registry links providers are installed from |
| **Health** | Whether the lyrics providers are reachable |
| **Command** | The equivalent `spotiflac …` command, rebuilt as you type |

Along the bottom: a status line, and a log pane that appears when a run
starts (`Ctrl+L` toggles it).

## How it looks

The design follows [MovieBox-Tui](https://github.com/mesamirh/MovieBox-Tui),
with the same grammar throughout:

- a **block-letter wordmark** across the top, with the version tucked under
  its right edge;
- **rounded cards** whose titles say what is in them and which one is live —
  `● Tracks · 3/5` on the pane the keyboard is pointing at, plain on the
  rest, with a lit border to match — and a tag in the corner naming the flag
  or key that does the same job;
- **badges** — a short label on a solid colour — for the quality tier and for
  each track's outcome, so neither depends on colour alone to be read;
- **`[key] action` hints** along the bottom with the key picked out in
  colour, dropped from the right as the terminal narrows;
- **toasts** in the bottom-right for things that just happened, labelled
  `DONE` / `WARNING` / `ERROR`, stacked and self-dismissing;
- a **status line** for what is true right now — how a run is going, what is
  still missing. The two are deliberately separate: a message that scrolls
  the standing state away has cost you the thing you were watching.

Nine themes, cycled with `t`, starting at Catppuccin Mocha: Mocha, Latte,
Macchiato, Frappé, Nord, Tokyo Night, Dracula, Gruvbox, Rosé Pine. Every
colour on the screen is a theme variable, so all nine look deliberate rather
than one looking right and eight looking tinted.

Everything degrades. `NO_COLOR`, `TERM=dumb`, or `SPOTIFLAC_PLAIN_TUI=1`
switch the block art for a word, the box glyphs for ASCII, and the filled
badges for `[LOSSLESS]`. The wordmark also shrinks on its own: six rows of
letterform become two on a narrow terminal, and a word below that — a logo
that eats a quarter of a short screen is a logo in the way.

Two things from MovieBox are deliberately not copied. It has **no sidebar** —
its screens are a linear flow (search, then details, then play) navigated with
`Tab`, where this has seven panels that are all live at once and a list of
them is the honest way to show that. And its posters are **images**, which a
terminal cannot draw without sixel support that most do not have.

## Keys

| Key | Does |
| --- | --- |
| `Ctrl+R` | Start the download |
| `Ctrl+C` | Stop a running download (or quit when nothing is running) |
| `Ctrl+L` | Show or hide the log pane |
| `Esc` | Close the log pane |
| `/` | Jump to the search box |
| `space` | On the Tracks panel: pick the track under the cursor |
| `a` / `n` / `i` | On the Tracks panel: all, none, invert |
| `t` | Cycle the theme (nine of them) |
| `?` | The key list, and a note on why settings go grey |
| `q` | Quit |
| `Tab` / `Shift+Tab`, `j` / `k` | Move between controls |

The mouse works too — clicking a sidebar entry, a switch or a provider is
often quicker than tabbing to it.

## The Download panel

Settings are grouped into sections you can fold away. Only **Source**,
**Destination** and **Providers & quality** are open to begin with, because
they are the three the run cannot start without; the banner at the bottom
says which of them is still missing.

Settings that currently have no effect are greyed out rather than hidden, so
you can see *why* they are unavailable:

- a **bitrate** only applies to a lossy conversion target;
- an **artist separator** only applies when you are keeping more than the
  first artist;
- **artist and album subfolders** are unavailable while track numbering is
  on, and vice versa — they are two ways of organising the same library;
- the **`.lrc` settings** are unavailable when lyrics are off;
- **playlist subfolders** only apply to a playlist URL.

These are the same rules the wizard enforced by skipping questions. Here you
can see all of them at once.

### Quality

Three tiers, and no more:

| Tier | What it means |
| --- | --- |
| **HI_RES_LOSSLESS** | The best each provider has |
| **LOSSLESS** | CD-quality FLAC/ALAC |
| **DOLBY ATMOS** | Tidal only — offered only when Tidal is one of your providers |

The canonical list in the code has six, but the other three are not choices
worth making: `HI_RES` is a Qobuz-only spelling of the same thing, and `HIGH`
and `LOW` are lossy tiers in a tool whose point is lossless. A saved profile
carrying one of them is read as the tier that means the same thing.

**Dolby Atmos** is a Tidal-exclusive stream. Pick it alongside other
providers and Tidal serves Atmos while the rest serve their best lossless —
which is what the command line already does, in
`core.quality.quality_for_provider`. With Tidal not selected at all it means
nothing, so it disappears from the menu and the setting falls back to Hi-Res
Lossless.

*Allow quality fallback* — on by default — covers a tier a given provider
will not serve.

### Where downloads land

`~/Music/SpotiFLAC`, the same folder the desktop window uses, unless you type
somewhere else in **Folder**. It stays the default: a one-off download
somewhere else does not quietly become where everything goes next.

### A CSV instead of a URL

Put the path to a `.csv` or `.tsv` in the **CSV track list** field, or press
**Browse for a track list…** and pick one: it lists what it finds in the
folders an export usually lands in, and shows the track count and the first
few titles before accepting the file — which is how you notice you picked
last month's export.

A track list takes precedence over the URL, which is greyed out while one is
set. See the [CSV page](csv.md) for what the file may contain.

## The Search panel

`/` from anywhere puts you in the search box. Results come back as tracks,
albums, artists and playlists; picking one makes its link the URL in the
Download panel and takes you there.

It does one thing on purpose. A search panel that also started downloads
would be a second copy of the Download panel's rules, and the two would drift
apart — so this one answers "which link did you mean" and hands the answer
over.

## The Tracks panel

The one thing the command line cannot do: fetch part of an album.

Press **Load tracks** and it reads what is behind the URL — title, artist,
album, one row each. Everything starts selected, because the panel exists to
take tracks away rather than to make you add them one at a time. `space`
toggles the row under the cursor, `a` selects all, `n` none, `i` inverts.

Loading is deliberate, not automatic: it is a network round trip against a
link you may still be typing.

Selecting **everything** downloads the collection URL itself, exactly as it
would without this panel — one resolution, and the album's own ordering and
numbering. Selecting **some** downloads those tracks individually. A track
whose provider gives no link of its own says `no link` in the album column;
it can be fetched as part of the whole collection, but not on its own, and
the log names any that get skipped.

Picking tracks has no command-line equivalent, and the **Command** panel says
so rather than showing you a command that would fetch the whole album.

## The Queue panel

One row per track, with a bar, fed by the same structured progress events the
desktop GUI consumes — there is no console output being scraped behind the
scenes. Finished tracks keep their row rather than disappearing, so at the
end you can still see which ones failed and why.

`Ctrl+R` starts the run and switches you here.

## The Session panel

**Recent URLs** — the last dozen things you fetched. Pick one and it fills
the URL field.

**Profiles** — the same profiles `--profile` and `--save-profile` use on the
command line. Select one to load it (the whole form is rebuilt from it), or
type a name and press **Save** to store the configuration you have now.

## The Extensions panel

Registry links are where providers come from; without at least one there is
nothing to download with. The table says where each link came from, which
matters: one exported in your terminal or written into a `.env` file comes
back when the process restarts, so removing it here would not stick.

Adding a link installs from it straight away rather than at the next launch —
the reason to add a registry is always the provider you wanted a minute ago.
`--min-trust-tier` on the command line is honoured by that install.

## The Health panel

Probes the lyrics providers directly and says which answered. The wizard ran
this before its first question; here it is a button, because it is something
you need when lyrics come back empty rather than on every single run —
opening the panel does not start any network traffic on its own.

`--health-check` does the same thing from the command line.

## The Command panel

The `spotiflac …` invocation that would do exactly what the form is set up to
do, updated on every change. Only settings that differ from the defaults
appear, so it stays readable.

This is the way out of the guided mode: once a configuration is one you run
often, copy the command into a script or a cron job and stop opening the UI
for it.

## Coming from the wizard

`--interactive` opened a sequence of questions and produced a configuration
at the end. That wizard has been removed; `--interactive` is now just a
deprecated spelling of `--tui`, which holds the same configuration as state
you can edit in any order. There is no "going back" — every answer is always
visible and always changeable.

Two things worked differently and are worth knowing about:

- **Quality** is three tiers rather than the wizard's provider-by-provider
  question. See [Quality](#quality) below.
- **The health check** no longer runs before every session. It is the
  **Health** panel, on demand.
- **The extension registry menu** is the **Extensions** panel. The registry
  flags on the [Extensions](extensions.md) page still do the same job from a
  script.
- **Browsing for a CSV** works the same way, from the **Browse for a track
  list…** button under the *CSV track list* field.

If your terminal cannot host a full-screen UI — a pipe, a heredoc, a screen
reader, a very limited terminal — the answer is the command line rather than
a second wizard. It is better suited to all four, and the **Command** panel
exists to hand you the invocation.

## Not enough room

The TUI wants roughly 80×24. Below that Textual will still draw, but panels
start to crowd. The command line has no such requirement.
