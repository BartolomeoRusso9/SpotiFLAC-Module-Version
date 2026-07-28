#!/usr/bin/env sh
set -e

# 1. Start Xvfb virtual screen (MANDATORY: required by Chromium to prevent crashing)
Xvfb :99 -screen 0 1280x900x24 -ac +extension GLX +render -noreset &
sleep 1

# ==============================================================================
# [OPTIONAL - VNC/WEB SCREEN]:
# To view Chromium's screen live in your browser or via VNC:
# 1. Make sure you uncommented the packages (fluxbox, x11vnc, novnc, websockify) and EXPOSE in the Dockerfile.
# 2. Uncomment the 3 commands below.
# 3. Run Docker with the port flag mapped: -p 6080:6080
# 4. Open your browser at: http://localhost:6080/vnc.html
#
# Example Command:
# docker run --rm -it \
#   -p 6080:6080 \
#   -v "$(pwd)/downloads:/app/downloads" \
#   -v "$(pwd)/.spotiflac_docker:/root/.spotiflac" \
#   -v "$(pwd)/.cache_docker:/root/.cache/spotiflac" \
#   --shm-size=1g \
#   spotiflac "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT" \
#   /app/downloads -s amazon -v
# ==============================================================================

# 2. Start Fluxbox window manager to keep Chromium windows organized
# fluxbox -display :99 >/dev/null 2>&1 &

# 3. Start VNC server on port 5900 (no password, for local access)
# x11vnc -display :99 -forever -nopw -shared -bg -quiet

# 4. Start noVNC bridge to view the screen from a web browser on port 6080
# websockify --web=/usr/share/novnc --daemon 6080 localhost:5900 >/dev/null 2>&1

export TS_DEBUG_VISIBLE=1

if [ "$#" -eq 0 ]; then
  echo "SpotiFLAC Docker image: pass a URL and output directory as arguments."
  echo "Example:"
  echo "  docker run --rm -it \\"
  echo "    -p 6080:6080 \\"
  echo "    -v \"\$(pwd)/downloads:/app/downloads\" \\"
  echo "    -v \"\$(pwd)/.spotiflac_docker:/root/.spotiflac\" \\"
  echo "    -v \"\$(pwd)/.cache_docker:/root/.cache/spotiflac\" \\"
  echo "    --shm-size=1g \\"
  echo "    spotiflac \"https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT\" \\"
  echo "    /app/downloads -s amazon -v"
  echo
  exec spotiflac --help
fi

exec python /app/launcher.py "$@"