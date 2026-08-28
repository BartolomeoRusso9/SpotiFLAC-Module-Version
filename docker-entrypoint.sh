#!/usr/bin/env sh
set -e

# ==============================================================================
# [WEB MODE]: if --web is among the arguments, skip Xvfb/Fluxbox/VNC entirely —
# the FastAPI/uvicorn server needs no virtual display. Map the app's own port
# instead (default 8000), not 6080/5900 (those are for the old VNC-based GUI).
#
# Example:
# docker run --rm -it \
#   -p 8000:8000 \
#   -e SPOTIFLAC_REGISTRIES=https://example.com/my-registry.json \
#   -v "$(pwd)/downloads:/app/downloads" \
#   -v "$(pwd)/.spotiflac_docker:/home/spotiflac/.spotiflac" \
#   -v "$(pwd)/.cache_docker:/home/spotiflac/.cache/spotiflac" \
#   spotiflac --web --host 0.0.0.0 --port 8000
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
#   -v "$(pwd)/.spotiflac_docker:/home/spotiflac/.spotiflac" \
#   -v "$(pwd)/.cache_docker:/home/spotiflac/.cache/spotiflac" \
#   --shm-size=1g \
#   spotiflac "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT" \
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
  echo "SpotiFLAC Docker image: pass a URL and output directory as arguments,"
  echo "or run the web GUI with: spotiflac --web --host 0.0.0.0 --port 8000"
  echo "Example (CLI download):"
  echo "  docker run --rm -it \\"
  echo "    -p 6080:6080 \\"
  echo "    -v \"\$(pwd)/downloads:/app/downloads\" \\"
  echo "    -v \"\$(pwd)/.spotiflac_docker:/home/spotiflac/.spotiflac\" \\"
  echo "    -v \"\$(pwd)/.cache_docker:/home/spotiflac/.cache/spotiflac\" \\"
  echo "    --shm-size=1g \\"
  echo "    spotiflac \"https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT\" \\"
  echo "    /app/downloads -s amazon -v"
  echo
  exec spotiflac --help
fi

exec python /app/launcher.py "$@"