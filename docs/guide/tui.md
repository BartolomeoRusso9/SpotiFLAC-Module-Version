# Terminal UI (`--tui`)

The guided mode. One screen instead of fifteen questions, and the download
queue live in front of you while it runs.

```bash
spotiflac --tui
```

*(Or `python launcher.py --tui` if running from source.)*

`--interactive` still works and opens this same screen, but it warns that it
is deprecated and it will be removed in a future release. See
[Coming from the wizard](#coming-from-the-wizard) below.

---

## What is on the screen

A sidebar on the left picks the panel; everything else is that panel.

| Panel | What it is for |
| --- | --- |
| **Download** | Every setting, all editable, in whatever order you like |
| **Search** | Find something in the catalogue and make it the URL |
| **Queue** | The live run: one bar per track, plus the totals |
| **Session** | Recent URLs and saved profiles |
| **Extensions** | The registry links providers are installed from |
| **Health** | Whether the lyrics providers are reachable |
| **Command** | The equivalent `spotiflac …` command, rebuilt as you type |

Along the bottom: a status line, and a log pane that appears when a run
starts (`Ctrl+L` toggles it).

## Keys

| Key | Does |
| --- | --- |
| `Ctrl+R` | Start the download |
| `Ctrl+C` | Stop a running download (or quit when nothing is running) |
| `Ctrl+L` | Show or hide the log pane |
| `/` | Jump to the search box |
| `t` | Cycle the theme |
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
at the end. `--tui` holds the same configuration as state you can edit in any
order, so there is no "going back" — every answer is always visible and
always changeable.

Two things worked differently and are worth knowing about:

- **Quality** is offered as the six canonical tiers rather than a menu
  tailored to the providers you picked. The wizard could narrow the question
  because it had already asked about providers; a screen where both are
  editable at once cannot. *Allow quality fallback* — on by default — covers
  a tier a given provider will not serve.
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
