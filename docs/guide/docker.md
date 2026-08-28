<!-- Extracted verbatim from README.md. The README had grown to 76 KB
     and 87 headings, which is past the point where either GitHub or
     PyPI renders it usefully. Nothing here was reworded in the split. -->

[← Back to the README](../../README.md)

# Docker & Headless Automation

## Docker Usage & Headless Automation

A lightweight, CLI-focused Docker image is available for running SpotiFLAC on servers, NAS devices, or any headless environment.

### Build the Image

```bash
docker build -t spotiflac .
```

### Basic Docker Usage

The image runs a virtual display (Xvfb) and exposes it over VNC — some installed extensions may rely on a headless browser internally. Map port `6080` (web VNC viewer) and set `--shm-size=1g`, or the browser-dependent parts may crash:

Run a download by mounting local directories to persist your downloads, configuration, cache, and extension registry across container restarts. Remember to also pass `SPOTIFLAC_REGISTRIES` (via `-e` or an `.env` file) since none is configured by default:

```bash
docker run --rm -it \
  -p 6080:6080 \
  --shm-size=1g \
  -e SPOTIFLAC_REGISTRIES="https://example.com/my-registry.json" \
  -v "$(pwd)/downloads:/app/downloads" \
  -v "$(pwd)/.spotiflac_docker:/root/.spotiflac" \
  -v "$(pwd)/.cache_docker:/root/.cache/spotiflac" \
  spotiflac "https://open.spotify.com/track/TRACK_ID" \
  /app/downloads -s ext:deezer-web -q LOSSLESS
```

Open `http://localhost:6080/vnc.html` in a browser to watch the virtual screen live, if needed. Set `X11VNC_PASSWORD` (env var, see `.env.example`) to protect the VNC session with a password; if unset, it starts without one.

### Web Mode in Docker (lighter alternative to VNC)

If you just want the GUI itself over the network — not a live view of a virtual desktop — `--web` mode needs none of the above. The entrypoint detects `--web` and skips Xvfb/Fluxbox/VNC entirely, so the container starts faster and uses less memory:

```bash
docker run --rm -it \
  -p 8000:8000 \
  -e SPOTIFLAC_REGISTRIES="https://example.com/my-registry.json" \
  -v "$(pwd)/downloads:/app/downloads" \
  -v "$(pwd)/.spotiflac_docker:/root/.spotiflac" \
  -v "$(pwd)/.cache_docker:/root/.cache/spotiflac" \
  spotiflac --web --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` in a browser.

> **Note:** `--host 0.0.0.0` is required here — the CLI default (`127.0.0.1`) would only accept connections from inside the container itself, unreachable from the host. This also means the GUI is reachable by anything that can reach the mapped port, with no authentication unless you add `--web-token` / `--web-multiuser` (see [Authentication](quick-start.md#authentication---web-token)). Only publish the port on a network you trust, or put it behind your own authentication/reverse proxy.

`docker-compose.example.yml` in the repo root does the above as a compose file, plus a real HTTP healthcheck for this specific mode (`docker compose -f docker-compose.example.yml up`).

### Published Image (GHCR)

Official Docker images are published on GitHub Container Registry (GHCR), allowing you to run the latest version without building locally.

```bash
docker pull ghcr.io/bartolomeorusso9/spotiflac-module-version:latest
```

### Logs in Headless Environments

A progress bar is a stream of carriage returns: readable on a terminal, unreadable in a log file. `docker logs` collapses each refresh into a `[285B blob data]` line, which buries everything worth reading.

SpotiFLAC therefore draws animated bars only when stderr is an interactive terminal. Everywhere else — Docker, cron, a redirected file — it prints the same information as plain lines instead:

```text
[RUN] 24 track(s) · ext:tidal-web, ext:qobuz-web · LOSSLESS · 2 in parallel → /app/downloads
Track [3/24] Track Title — Artist Name (Album Name)
  ⬇  Track Title  ·  47%  ·  13.4 MB / 28.4 MB
  ✓  Track Title  ·  TIDAL-WEB  ·  FLAC  ·  28.4 MB  ·  12s
```

Progress lines are throttled to at most one per 25% and per 10 seconds, so a track costs a handful of lines rather than one per received chunk.

Set `SPOTIFLAC_PROGRESS_BARS` to override the detection in either direction:

```bash
export SPOTIFLAC_PROGRESS_BARS=0   # never draw bars, even on a terminal
export SPOTIFLAC_PROGRESS_BARS=1   # always draw bars
```

---
