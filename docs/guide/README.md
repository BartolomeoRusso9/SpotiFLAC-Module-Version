# SpotiFLAC guide

The pages here were split out of the project README, which had reached 76 KB
and 87 headings — past the point where either GitHub or PyPI renders it
usefully. The text is unchanged by the split.

| Page | What's in it |
| --- | --- |
| [Quick Start](quick-start.md) | Every way to run it — GUI, web, CLI, terminal UI, Python — and the URL types it accepts |
| [Terminal UI](tui.md) | `--tui`, the guided mode: every setting on one screen, and the download queue live |
| [Extensions](extensions.md) | Registries, installing providers, signature verification and trust tiers |
| [Writing a Python Extension](python-extensions.md) | Building a download-provider extension: `BaseProvider`, the `core` toolbox, a full example, packaging and publishing |
| [Downloading from a CSV](csv.md) | Feeding it a file of tracks instead of a link: supported exports, how rows without a link are matched, and the unmatched-row report |
| [Your library in numbers](dashboard.md) | The dashboard built from what you have actually downloaded — top artists, genres, decades, activity |
| [Configuration](configuration.md) | Every option: naming, quality, lyrics, enrichment, transcoding, watch mode, profiles |
| [API Reference](api-reference.md) | `SpotiFLAC` and `AsyncSpotiFLAC`, and the objects they return |
| [Local Tagging](local-tagging.md) | Retagging an existing library, finding and resolving duplicates, MusicBrainz enrichment, download validation |
| [Automation & Operations](automation.md) | JSON output, post-download hooks, notifications, M3U, library rescan, cache maintenance, running the web server |
| [Following Artists](subscriptions.md) | Subscriptions, new-release checks, and the library-upgrade pass |
| [REST API](rest-api.md) | The versioned `/api/v1` surface, quotas and the admin endpoints |
| [Docker](docker.md) | Headless and NAS setups |
| [CLI](cli.md) | The standalone executables |

Elsewhere in the repo:

- [SECURITY.md](../../SECURITY.md) — reporting a vulnerability, and what is
  deliberately out of scope
- [CONTRIBUTING.md](../../CONTRIBUTING.md)
