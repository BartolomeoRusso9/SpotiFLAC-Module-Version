# SpotiFLAC Python Module

Fetch Spotify track metadata and retrieve matching lossless audio through Tidal, Qobuz, Amazon Music and other provider backends — supplied entirely via extensions you choose and configure yourself. Integrate directly into your Python projects, build custom Telegram bots, automation tools, or bulk downloaders.

[![GitHub stars](https://img.shields.io/github/stars/BartolomeoRusso9/SpotiFLAC-Module-Version?color=ffcb47&labelColor=black&logo=github&label=Stars)](https://github.com/BartolomeoRusso9/SpotiFLAC-Module-Version/stargazers)
[![Latest release](https://img.shields.io/github/v/release/BartolomeoRusso9/SpotiFLAC-Module-Version?color=8b5cf6&labelColor=black&logo=github&label=Latest%20Release)](https://github.com/BartolomeoRusso9/SpotiFLAC-Module-Version/releases/latest)
[![PyPI version](https://img.shields.io/pypi/v/spotiflac?logo=pypi&logoColor=ffffff&labelColor=000000&color=7b97ed)](https://pypi.org/project/SpotiFLAC/)
[![Python versions](https://img.shields.io/pypi/pyversions/spotiflac?logo=python&logoColor=ffffff&labelColor=000000&color=7b97ed)](https://pypi.org/project/SpotiFLAC/)
[![GitHub downloads](https://img.shields.io/github/downloads/BartolomeoRusso9/SpotiFLAC-Module-Version/total?color=22c55e&labelColor=black&logo=github&label=Downloads)](https://github.com/BartolomeoRusso9/SpotiFLAC-Module-Version/releases)
[![PyPI downloads](https://img.shields.io/pepy/dt/spotiflac?logo=pypi&logoColor=ffffff&labelColor=000000)](https://pypi.org/project/SpotiFLAC/)
[![Telegram community](https://img.shields.io/badge/Telegram%20Community-369eff?labelColor=black&logo=telegram&logoColor=white)](https://t.me/SpotiFLAC_Chat)

## Disclaimer

This project is intended for **educational and personal use only**. The developer does not condone or encourage copyright infringement. The software is licensed under the [MIT License](https://github.com/BartolomeoRusso9/SpotiFLAC-Module-Version/blob/main/LICENSE).

**SpotiFLAC-Module-Version** is an independent, third-party tool and is not affiliated with, endorsed by, or connected to Spotify, Tidal, Qobuz, Amazon Music, Deezer, or any other streaming service. It is also not affiliated with, and has no control over or responsibility for, any other project sharing a similar name on other platforms.

No copyrighted content is hosted, stored, mirrored, or distributed by this repository. The core application does not bundle, ship, or default to any third-party extension, registry, or provider. Extensions are installed only if a user explicitly configures and requests them from a source of their own choosing; the maintainer has no control over, does not review, and assumes no responsibility for the content or behavior of third-party extensions or registries a user may choose to install.

You are solely responsible for:

1. Ensuring your use of this software, and any extension or registry you choose to install, complies with your local laws.
2. Reading and adhering to the Terms of Service of any platform or provider you access, directly or through an extension.
3. Any legal consequences resulting from the use or misuse of this tool.

This software is provided free of charge by the maintainer. If you paid a third party for access to it, you may have been misled or scammed.

The software is provided "as is", without warranty of any kind, express or implied. The author assumes no liability for any bans, damages, or legal issues arising from its use or misuse. Users assume all risk associated with its use.

If you are a copyright holder or an authorized representative and believe this repository infringes upon your rights, please contact the maintainer with sufficient detail (including relevant URLs and proof of ownership); the matter will be promptly investigated.

---

> **Looking for a standalone app?**
>
> - [SpotiFLAC (Desktop)](https://github.com/afkarxyz/SpotiFLAC) — Download music in true lossless FLAC from different providers for Windows, macOS & Linux
> - [SpotiFLAC (Mobile)](https://github.com/zarzet/SpotiFLAC-Mobile) — SpotiFLAC for Android & iOS, maintained by [@zarzet](https://github.com/zarzet)

---

## Why the module (instead of the standalone apps)

The Desktop and Mobile apps are built for direct, immediate use: open it, paste a link, download. The Python module exists for a different case — **integrating** this logic into something else.

It makes sense to start here if:

- **You're building a bot** (Telegram, Discord) or a service that needs to handle requests from many users automatically, not a single manual download.
- **You need the async API** (`AsyncSpotiFLAC`) inside an existing FastAPI/Quart/Sanic app, with shared connection pooling.
- **You want to orchestrate bulk downloads** via scripts — multiple playlists, full discographies, local library retagging — with custom logic (filenames via a Python function, post-download actions, saved profiles).
- **You need to run headless**, on a server, a NAS, or inside Docker, possibly as part of a larger pipeline.
- **You want full control over which extensions get loaded** and from which registry, instead of relying on a fixed set baked into an app.

If you just want a GUI for personal use, with no code involved, the [Desktop](https://github.com/afkarxyz/SpotiFLAC) or [Mobile](https://github.com/zarzet/SpotiFLAC-Mobile) apps remain the simpler choice — this module is the building block those (and similar projects) can be built on top of.

---

## Features

- Native synchronous and asynchronous Python APIs
- Modular JavaScript and Python Extension system (bring-your-own registry — nothing bundled)
- Automatic fallback among the extensions *you* have installed
- Built-in GUI, as a native window or served locally in a browser (`--gui` / `--web`)
- Interactive CLI Wizard
- Docker support
- Configuration Profiles
- MusicBrainz metadata enrichment
- Embedded synchronized lyrics
- Optional MP3 320 kbps transcoding
- Watch mode: re-sync a playlist/album/artist/URL on an interval, indefinitely
- Optional shared-secret or per-account authentication for `--web`, plus a queued, per-user download history in multi-user mode
- Extension discovery directories, and optional Ed25519 signature verification on top of registry checksums
- Local-library duplicate detection by acoustic fingerprint, independent of tags/ISRC
- Extension scaffolding + dry-run validation for developing your own
- Installable as a PWA in `--web` mode (Add to Home Screen / standalone window)

---

## Installation

```bash
pip install SpotiFLAC
```

> **Important:** out of the box, SpotiFLAC does nothing but resolve Spotify metadata — it ships with **no built-in provider and no default extension source**. Before you can download anything, you need to point it at an extension registry of your own choosing and install at least one extension. See [Extensions](docs/guide/extensions.md#extensions) below.

---

## Documentation

The full guide lives in [`docs/guide/`](docs/guide/) — the README had grown
to 76 KB and 87 headings, which is past the point where GitHub or PyPI
renders it usefully. Every page below is the same text that used to be here.

| Page | What's in it |
| --- | --- |
| [Quick Start](docs/guide/quick-start.md) | Every way to run it — GUI, web, CLI, interactive wizard, Python — and the URL types it accepts |
| [Extensions](docs/guide/extensions.md) | Registries, installing providers, signature verification and trust tiers |
| [Configuration](docs/guide/configuration.md) | Every option: naming, quality, lyrics, enrichment, transcoding, watch mode, profiles |
| [API Reference](docs/guide/api-reference.md) | `SpotiFLAC` and `AsyncSpotiFLAC`, and the objects they return |
| [Local Tagging](docs/guide/local-tagging.md) | Retagging an existing library, MusicBrainz enrichment, download validation |
| [Automation & Operations](docs/guide/automation.md) | JSON output, post-download hooks, M3U, library rescan, cache maintenance, running the web server |
| [Docker](docs/guide/docker.md) | Headless and NAS setups |
| [CLI](docs/guide/cli.md) | The standalone executables |

Security policy and the reporting process: [SECURITY.md](SECURITY.md).

---

## Want to support the project?

If this software is useful and brings you value, consider supporting the project by buying us a coffee. Your support helps keep development going.

[![Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/bartolomeorusso9)

---

## API Credits

[Song.link](https://song.link) · [MusicBrainz](https://musicbrainz.org) · [LRCLIB](https://lrclib.net) · [Musixmatch](https://www.musixmatch.com) · [iTunes Search API](https://itunes.apple.com)

> Provider-specific credits (Tidal, Qobuz, Amazon Music, Deezer, SoundCloud, Apple Music, Pandora, and any third-party API used to reach them) now belong to whichever extension you install — see that extension's own documentation for its credits and terms.
>
> **[!TIP]** Star the repo to show support, and click **Watch → Custom → Releases** on GitHub if you want to be notified as soon as a new release goes out.
