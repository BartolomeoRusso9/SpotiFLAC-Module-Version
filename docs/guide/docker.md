<!-- Extracted verbatim from README.md. The README had grown to 76 KB
     and 87 headings, which is past the point where either GitHub or
     PyPI renders it usefully. Nothing here was reworded in the split. -->

[← Back to the README](../../README.md)

# Docker & Headless Automation

## Docker Usage & Headless Automation

A Docker image is available for running Mornye on servers, NAS devices, or any headless environment. This page covers, in order: running it with Docker Compose, who the container runs as, where it keeps things, every environment variable it understands, and the `docker run` equivalents.

### Quick Start (Docker Compose)

The usual way to run Mornye on a server is `--web` mode: the GUI served over HTTP, with no virtual desktop involved.

```yaml
services:
  mornye:
    image: ghcr.io/mornyeapp/mornye-python:latest
    command: ["--web", "--host", "0.0.0.0", "--port", "8000"]
    user: "${PUID:-1000}:${PGID:-1000}"
    ports:
      - "8000:8000"
    shm_size: "1gb"
    environment:
      MORNYE_REGISTRIES: "https://example.com/my-registry.json"
      MORNYE_WEB_TOKEN: "a-long-random-string"
      MORNYE_DOWNLOAD_DIR: /app/downloads
      MORNYE_DATA_DIR: /home/mornye/.mornye
      MORNYE_PROGRESS_BARS: "0"
    volumes:
      - ./downloads:/app/downloads
      - ./mornye-data:/home/mornye/.mornye
    restart: unless-stopped
```

```bash
mkdir -p downloads mornye-data
docker compose up -d
```

Then open `http://localhost:8000`.

- `--host 0.0.0.0` is required: the default (`127.0.0.1`) only accepts connections from inside the container. It also means the GUI is reachable by anything that can reach the published port, so set `MORNYE_WEB_TOKEN` (or use `--web-multiuser`, see [Authentication](quick-start.md#authentication---web-token)) unless the port never leaves a network you fully trust.
- `MORNYE_REGISTRIES` is the one variable you cannot skip: no extension registry is bundled, and without one there are no providers to download with.
- `shm_size: "1gb"`: a download can hit a Turnstile challenge, which launches a real Chromium. It crashes on Docker's default 64 MB `/dev/shm`.

Two ready-made files live in the repository root:

| File | Use it for |
| --- | --- |
| [`docker-compose.example.yml`](../../docker-compose.example.yml) | A minimal stack that builds from source, with an HTTP healthcheck |
| [`docker-compose.portainer.yml`](../../docker-compose.portainer.yml) | The published image, every variable below wired up, plus Watchtower auto-updates |

### User and Permissions (PUID / PGID)

The image runs as an unprivileged user, UID/GID `1000`, by default. There are two ways to change that; pick by whether the mounted directories are already writable by the user you want.

**Rootless — `user:`.** The compose file above passes `user: "${PUID:-1000}:${PGID:-1000}"`, so setting `PUID` and `PGID` (in a `.env` file next to the compose file, in your shell, or in Portainer's stack variables) is all it takes:

```bash
# .env
PUID=1026
PGID=100
```

Any UID/GID works, including one with no entry in the container's `/etc/passwd`. Nothing ever runs as root. The mounted directories must already be writable by that user — true for a bind mount you created yourself, not for a named volume or a directory Docker created for you (those start out owned by `1000` or by root).

**Root-assisted — `user: "0:0"` plus `PUID`/`PGID` as container variables.** For the cases the rootless form cannot handle:

```yaml
    user: "0:0"
    environment:
      PUID: "1026"
      PGID: "100"
```

The entrypoint starts as root only long enough to hand the data directory to `PUID:PGID`, then drops to them permanently before starting anything else — Mornye, its extensions and the browser never run as root. The download directory is chowned at the top level only, never recursively: it may be a whole music library, on storage (NFS, mergerfs) where root is not allowed to chown at all, and a failure there is not treated as an error.

In this mode `docker exec` runs as root unless told otherwise, and anything it creates in the data directory will be root-owned. Pass the user explicitly:

```bash
docker exec -u 1026:100 mornye mornye --web-user-add NAME PASS
```

`user: "0:0"` *without* `PUID`/`PGID` runs everything as root. It works, and it is not recommended: the container installs and executes third-party extensions.

### Volumes and Locations

Nothing Mornye writes depends on `$HOME`; each location is a variable, and each should be (inside) a mounted volume.

| What | Variable | Default in the image |
| --- | --- | --- |
| Downloaded music (`--web`) | `MORNYE_DOWNLOAD_DIR` | `/home/mornye/Music/Mornye` |
| Database, extensions, signed sessions, web users, trusted keys, settings | `MORNYE_DATA_DIR` | `/home/mornye/.mornye` |
| Cache (regenerable) | `MORNYE_CACHE_DIR` | `$MORNYE_DATA_DIR/.cache` |
| Database file only | `MORNYE_DB_PATH` | `$MORNYE_DATA_DIR/mornye.db` |

> **Set `MORNYE_DOWNLOAD_DIR`.** Without it `--web` saves to `~/Music/Mornye`, which is inside the container's writable layer unless you mount exactly that path — every download disappears the next time the container is recreated. The web folder picker cannot fix this afterwards: it is confined to `$HOME` and the current download directory.

To put everything on paths of your own choosing, change both sides together — the variable and the right-hand side of the volume:

```yaml
    environment:
      MORNYE_DOWNLOAD_DIR: /music
      MORNYE_DATA_DIR: /config
    volumes:
      - /mnt/media/music:/music
      - /srv/mornye/config:/config
```

In `--web-multiuser` mode each account gets its own subfolder of `MORNYE_DOWNLOAD_DIR`.

For SMB shares, choose either a host-mounted volume or the optional native
backend. The exact volume mapping, ownership requirements, and container
network configuration are in [Network Destinations](network-destinations.md#docker-network-destinations).

For CLI downloads the output folder is the second argument (`mornye URL /app/downloads`), and the variable is not involved.

**Lyrics and Canvas files.** Lyrics are embedded in the audio file; `--save-lrc` additionally writes an `.lrc` next to each track, so it lands in the download volume with no extra setup. A separate lyrics or Canvas folder (`--lrc-dir`, `--canvas-dir`, or the same settings in the GUI — see [Lyrics Files](configuration.md#lyrics-files-lrc)) takes a container path: mount a volume there.

**Read-only root filesystem.** `read_only: true` works as long as the data and download directories are volumes and `/tmp` is writable:

```yaml
    read_only: true
    tmpfs:
      - /tmp
```

When `$HOME` is not writable (a read-only root, or a UID other than 1000) the entrypoint points it at `/tmp/mornye-home` for the browser's benefit; Mornye's own data stays where `MORNYE_DATA_DIR` says.

**Temporary files.** The Turnstile browser gets a throwaway profile in `/tmp` (`ts_profile_*`, a few tens of MB), deleted as soon as the browser exits — including when it has to be force-killed. Profiles left behind by a crash, a `docker stop` mid-download, or an older release are removed the next time a browser is launched. `/tmp` on `tmpfs`, as above, additionally guarantees nothing survives a restart, at the cost of that space coming out of RAM.

### Environment Variables

Everything is optional except `MORNYE_REGISTRIES`. An empty value is the same as unset, so a compose file can pass `"${NAME:-}"` for all of them.

**Container**

| Variable | Default | Purpose |
| --- | --- | --- |
| `PUID`, `PGID` | `1000` | User and group to run as — see [User and Permissions](#user-and-permissions-puid--pgid) |
| `TZ` | UTC | Time zone, e.g. `Europe/Rome` |
| `X11VNC_PASSWORD` | unset | Enables the VNC / noVNC view of the virtual display (CLI and `--gui` modes only) and protects it with this password |

**Storage**

| Variable | Default | Purpose |
| --- | --- | --- |
| `MORNYE_DOWNLOAD_DIR` | `~/Music/Mornye` | Where `--web` (and the other interfaces, when no folder is given) saves music |
| `MORNYE_DATA_DIR` | `~/.mornye` | Database, extensions, signed sessions, web users, trusted keys, settings |
| `MORNYE_EXT_DIR` | `$MORNYE_DATA_DIR/extensions` | Installed extensions |
| `MORNYE_CACHE_DIR` | `$MORNYE_DATA_DIR/.cache` | Regenerable cache |
| `MORNYE_DB_PATH` | `$MORNYE_DATA_DIR/mornye.db` | The database file |

**Extensions**

| Variable | Default | Purpose |
| --- | --- | --- |
| `MORNYE_REGISTRIES` | none — **required** | Comma-separated registry JSON URLs |
| `MORNYE_REGISTRY_DIRECTORIES` | none | Directory JSONs that list registries for you to review (GUI › Discover) |
| `MORNYE_EXT_LINKS` | none | Path to a file of extension download URLs, one per line |
| `MORNYE_MIN_TRUST` | `unverified` | Minimum trust tier accepted on install: `unverified`, `checksum-only` or `signed` |
| `MORNYE_EXT_UPDATE_INTERVAL` | `3600` | Seconds between checks for newer extension versions; `0` = only at start |
| `MORNYE_ENRICH_EXTENSIONS` | on | `0` stops extensions from contributing metadata |

**Web GUI and automation**

| Variable | Default | Purpose |
| --- | --- | --- |
| `MORNYE_WEB_TOKEN` | none | Shared secret for the web GUI (same as `--web-token`) |
| `MORNYE_NOTIFY_URL`, `MORNYE_NOTIFY_TOKEN` | none | Fallbacks for `--notify-url` / `--notify-token` — see [Automation](automation.md) |
| `MORNYE_LIBRARY_USER`, `MORNYE_LIBRARY_TOKEN` | none | Music-server credentials for `--library-rescan` — see [Automation](automation.md) |
| `MORNYE_PROGRESS_BARS` | auto | `0` = plain log lines, `1` = animated bars — see [Logs](#logs-in-headless-environments) |

**Metadata and lyrics**

| Variable | Default | Purpose |
| --- | --- | --- |
| `MORNYE_APPLE_MEDIA_USER_TOKEN` | none | Apple Music user token, for Apple metadata and lyrics |
| `MORNYE_APPLE_STOREFRONT` | `us` | Apple Music storefront (country code) |
| `MORNYE_APPLE_LYRICS_TRANSLATION`, `MORNYE_APPLE_LYRICS_PRONUNCIATION` | off | Also fetch translated / romanised Apple lyrics |
| `MORNYE_LYRICS_JELLYFIN_COMPAT` | off | `1` keeps word-by-word lyrics readable in Jellyfin clients |

**Browser (Turnstile solver)**

| Variable | Default | Purpose |
| --- | --- | --- |
| `TS_MAX_CONCURRENT_BROWSERS` | `3` | Chromium instances allowed at once; each costs a few hundred MB of RAM while it runs |
| `TS_BROWSER_START_TIMEOUT` | `45` | Seconds to wait for Chromium to start |
| `TS_BROWSER_SLOT_TIMEOUT` | `120` | Seconds to wait for a free slot when all are busy |
| `TS_PROFILE_DIR` | unset | A persistent browser profile instead of a throwaway one. It is never cleaned up for you |
| `CHROME_PATH` | autodetected | Chromium-based browser to use (the image ships `chromium`) |
| `TURNSTILE_SOLVER_URL` | unset | An external Turnstile solver service to use instead of the built-in browser |

**Security escape hatches.** Each of these weakens a protection; all are off unless set to `1`/`true`/`yes`.

| Variable | Effect |
| --- | --- |
| `MORNYE_EXT_ENV_PASSTHROUGH` | Comma-separated environment variable names an extension may read (the sandbox strips the rest) |
| `MORNYE_EXT_ALLOW_PRIVATE_NETWORK` | Lets extensions reach private and localhost addresses |
| `MORNYE_EXT_ALLOW_ANY_WRITE`, `MORNYE_EXT_WRITABLE_DIRS` | Lets extensions write outside their own directories |
| `MORNYE_EXT_NO_SANDBOX` | Disables the extension sandbox entirely — debugging only |
| `MORNYE_ALLOW_CHECKSUM_MISMATCH` | Installs an extension whose checksum does not match the registry |
| `MORNYE_ALLOW_POST_COMMAND` | Lets the web GUI run a post-download shell command |
| `MORNYE_NO_LOG_REDACTION` | Stops redacting provider tokens out of HTTP logs |

### Published Image (GHCR)

The repository workflow is configured to publish images to GitHub Container Registry. Use the image after it has been published and is accessible to your account:

```bash
docker pull ghcr.io/mornyeapp/mornye-python:latest
```

To build from source instead:

```bash
docker build -t mornye .
```

### Using `docker run`

Everything above applies unchanged — `--user` for `user:`, `-e` for `environment:`, `-v` for `volumes:`.

**Web mode:**

```bash
docker run -d --name mornye \
  --user "$(id -u):$(id -g)" \
  -p 8000:8000 \
  --shm-size=1g \
  -e MORNYE_REGISTRIES="https://example.com/my-registry.json" \
  -e MORNYE_WEB_TOKEN="a-long-random-string" \
  -e MORNYE_DOWNLOAD_DIR=/app/downloads \
  -v "$(pwd)/downloads:/app/downloads" \
  -v "$(pwd)/mornye-data:/home/mornye/.mornye" \
  ghcr.io/mornyeapp/mornye-python:latest --web --host 0.0.0.0 --port 8000
```

**One-shot CLI download.** Without `--web` the container starts a virtual display (Xvfb) for the browser and runs a single command, then exits:

```bash
docker run --rm -it \
  --user "$(id -u):$(id -g)" \
  --shm-size=1g \
  -e MORNYE_REGISTRIES="https://example.com/my-registry.json" \
  -v "$(pwd)/downloads:/app/downloads" \
  -v "$(pwd)/mornye-data:/home/mornye/.mornye" \
  ghcr.io/mornyeapp/mornye-python:latest \
  "https://open.spotify.com/track/TRACK_ID" \
  /app/downloads -s ext:deezer-web -q LOSSLESS
```

**Watching the browser.** To see that virtual display live, set `X11VNC_PASSWORD`, publish port `6080` (`-p 6080:6080`) and open `http://localhost:6080/vnc.html`. Without the variable the VNC services are not started.

### Logs in Headless Environments

A progress bar is a stream of carriage returns: readable on a terminal, unreadable in a log file. `docker logs` collapses each refresh into a `[285B blob data]` line, which buries everything worth reading.

Mornye therefore draws animated bars only when stderr is an interactive terminal. Everywhere else — Docker, cron, a redirected file — it prints the same information as plain lines instead:

```text
[RUN] 24 track(s) · ext:tidal-web, ext:qobuz-web · LOSSLESS · 2 in parallel → /app/downloads
Track [3/24] Track Title — Artist Name (Album Name)
  ⬇  Track Title  ·  47%  ·  13.4 MB / 28.4 MB
  ✓  Track Title  ·  TIDAL-WEB  ·  FLAC  ·  28.4 MB  ·  12s
```

Progress lines are throttled to at most one per 25% and per 10 seconds, so a track costs a handful of lines rather than one per received chunk.

Set `MORNYE_PROGRESS_BARS` to override the detection in either direction:

```bash
export MORNYE_PROGRESS_BARS=0   # never draw bars, even on a terminal
export MORNYE_PROGRESS_BARS=1   # always draw bars
```

---
