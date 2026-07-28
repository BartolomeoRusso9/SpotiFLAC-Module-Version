#!/usr/bin/env sh
set -e

# Display virtuale
Xvfb :99 -screen 0 1280x900x24 &
sleep 1

export TS_DEBUG_VISIBLE=1


if [ "$#" -eq 0 ]; then
  echo "SpotiFLAC Docker image: pass a URL and output directory as arguments."
  echo "Example: docker run -it --rm \\"
  echo "    -v \$(pwd)/downloads:/app/downloads \\"
  echo "    -v \$(pwd)/sessions:/root/.spotiflac/signed_sessions \\"
  echo "    spotiflac https://open.spotify.com/track/... /app/downloads -s tidal -q LOSSLESS"
  echo
  exec spotiflac --help
fi

exec python /app/telegram_wrapper.py "$@"