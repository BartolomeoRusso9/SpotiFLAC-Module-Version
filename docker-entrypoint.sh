#!/usr/bin/env sh
set -e

# ==============================================================================
# [IDENTITY AND LOCATIONS]
#
# Three ways to choose who the container runs as, all supported:
#
#   1. Nothing set: the image's own `mornye` user, UID/GID 1000.
#   2. `user: "1234:1234"` (compose) / `--user 1234:1234` (docker run): any
#      UID/GID, no root involved at any point. The mounted directories must
#      already be writable by that UID.
#   3. `user: "0:0"` plus PUID/PGID: starts as root only long enough to make
#      the data directories belong to PUID:PGID, then drops to them for good
#      — nothing below this block, and nothing Mornye itself does, ever
#      runs as root. Use it when the volumes are not writable by the target
#      UID yet (a named volume, a freshly created bind mount).
#
# Where things go is independent of $HOME, so none of the three needs a home
# directory that exists or is writable:
#
#   MORNYE_DATA_DIR      database, extensions, sessions, settings
#                           (default /home/mornye/.mornye)
#   MORNYE_CACHE_DIR     regenerable cache (default $MORNYE_DATA_DIR/.cache)
#   MORNYE_DOWNLOAD_DIR  where --web saves music (default ~/Music/Mornye)
# ==============================================================================
: "${MORNYE_DATA_DIR:=${HOME:-/home/mornye}/.mornye}"
export MORNYE_DATA_DIR

if [ "$(id -u)" = "0" ] && [ -n "${PUID:-}${PGID:-}" ]; then
  run_uid="${PUID:-1000}"
  run_gid="${PGID:-1000}"
  if [ "$run_uid" != "0" ]; then
    # The data directory is small and entirely ours: hand over all of it, but
    # only when the top level says it has not been done already.
    for dir in "$MORNYE_DATA_DIR" "${MORNYE_CACHE_DIR:-}" "${MORNYE_EXT_DIR:-}"; do
      [ -n "$dir" ] || continue
      mkdir -p "$dir" 2>/dev/null || true
      if [ "$(stat -c '%u:%g' "$dir" 2>/dev/null)" != "$run_uid:$run_gid" ]; then
        chown -R "$run_uid:$run_gid" "$dir" 2>/dev/null \
          || echo "mornye: could not chown $dir to $run_uid:$run_gid" >&2
      fi
    done
    # The download directory may be a whole music library, on storage where
    # root is not even allowed to chown (NFS, mergerfs): top level only,
    # never recursive, and a failure is not an error.
    for dir in "${MORNYE_DOWNLOAD_DIR:-}" /app/downloads; do
      [ -n "$dir" ] || continue
      mkdir -p "$dir" 2>/dev/null || true
      chown "$run_uid:$run_gid" "$dir" 2>/dev/null || true
    done
    exec setpriv --reuid "$run_uid" --regid "$run_gid" --clear-groups "$0" "$@"
  fi
fi

# An arbitrary UID has no home of its own, and /home/mornye belongs to UID
# 1000 (or sits on a read-only root filesystem). Mornye's own data no longer
# depends on $HOME (pinned above), but Chromium, fontconfig and Node still
# expect one they can write to.
if [ ! -w "${HOME:-/nonexistent}" ]; then
  export HOME="${TMPDIR:-/tmp}/mornye-home"
  mkdir -p "$HOME" 2>/dev/null || true
fi

mkdir -p "$MORNYE_DATA_DIR" 2>/dev/null || true
if [ ! -w "$MORNYE_DATA_DIR" ]; then
  echo "mornye: $MORNYE_DATA_DIR is not writable by UID $(id -u):$(id -g)." >&2
  echo "mornye: chown the mounted directory on the host, or start the container" >&2
  echo "mornye: with user \"0:0\" and PUID/PGID set so it is fixed for you." >&2
fi

# ==============================================================================
# [WEB MODE]: if --web is among the arguments, skip Xvfb/Fluxbox/VNC here and
# map the app's own port instead (default 8000), not 6080/5900 (those are for
# the old VNC-based GUI).
#
# "Skip", not "never needs": uvicorn itself wants no display, but a download it
# serves can still have to solve a Turnstile challenge, and core/solver.py
# launches a real (non-headless) Chromium to do it. That browser needs an X
# server, so the solver brings one up on demand — which is cheaper than an
# Xvfb in every container that never goes near a challenge.
#
# Note the image sets DISPLAY=:99 for every mode (see the Dockerfile), so the
# solver has to check whether anything is actually listening there rather than
# trust the variable. It does; if you change either side, keep that true.
#
# Example:
# docker run --rm -it \
#   -p 8000:8000 \
#   -e MORNYE_REGISTRIES=https://example.com/my-registry.json \
#   -v "$(pwd)/downloads:/app/downloads" \
#   -v "$(pwd)/.mornye_docker:/home/mornye/.mornye" \
#   mornye --web --host 0.0.0.0 --port 8000
#
# Note: --host 0.0.0.0 is required here (not the CLI default 127.0.0.1),
# since otherwise the server only accepts connections from inside the
# container itself. That also means it's reachable by anything that can
# reach the mapped port — there is no authentication. Only publish the
# port on a network you trust, or put it behind your own auth/reverse proxy.
# ==============================================================================
for arg in "$@"; do
  if [ "$arg" = "--web" ]; then
    exec python /app/launcher.py "$@"
  fi
done

rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true
# 1. Start Xvfb virtual screen (MANDATORY: required by Chromium to prevent crashing)
Xvfb :99 -screen 0 1280x900x24 -ac +extension GLX +render -noreset &
sleep 1

# ==============================================================================
# [VNC/WEB SCREEN]:
# To view Chromium's screen live in your browser or via VNC:
# 3. Run Docker with the port flag mapped: -p 6080:6080
# 4. Open your browser at: http://localhost:6080/vnc.html
#
# Example Command:
# docker run --rm -it \
#   -p 6080:6080 \
#   -v "$(pwd)/downloads:/app/downloads" \
#   -v "$(pwd)/.mornye_docker:/home/mornye/.mornye" \
#   --shm-size=1g \
#   mornye "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT" \
#   /app/downloads -s amazon -v
# ==============================================================================

# 2. Start Fluxbox window manager to keep Chromium windows organized
fluxbox -display :99 >/dev/null 2>&1 &

# 3. Start VNC server on port 5900.
# Password is read from an environment variable (required for security).
# Example: X11VNC_PASSWORD=your_secure_password docker run ...
VNC_PASSWORD="${X11VNC_PASSWORD:-}"
if [ -n "$VNC_PASSWORD" ]; then
  x11vnc -display :99 -forever -passwd "$VNC_PASSWORD" -shared -bg -quiet
  # 4. Start noVNC bridge to view the screen from a web browser on port 6080
  websockify --web=/usr/share/novnc --daemon 6080 localhost:5900 >/dev/null 2>&1
else
  echo "VNC/noVNC services disabled: X11VNC_PASSWORD not set."
  echo "Set X11VNC_PASSWORD environment variable to enable screen viewing."
fi

export TS_DEBUG_VISIBLE=1

if [ "$#" -eq 0 ]; then
  echo "Mornye Docker image: pass a URL and output directory as arguments,"
  echo "or run the web GUI with: mornye --web --host 0.0.0.0 --port 8000"
  echo "Example (CLI download):"
  echo "  docker run --rm -it \\"
  echo "    -p 6080:6080 \\"
  echo "    -v \"\$(pwd)/downloads:/app/downloads\" \\"
  echo "    -v \"\$(pwd)/.mornye_docker:/home/mornye/.mornye\" \\"
  echo "    --shm-size=1g \\"
  echo "    mornye \"https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT\" \\"
  echo "    /app/downloads -s amazon -v"
  echo
  exec mornye --help
fi

exec python /app/launcher.py "$@"