#!/usr/bin/env sh
set -e

Xvfb :99 -screen 0 1280x1024x24 > /dev/null 2>&1 &
sleep 1

if [ "$#" -eq 0 ]; then
  echo "SpotiFLAC Docker image: pass a URL and output directory as arguments."
  echo "Example: docker run --rm \\"
  echo "    -v \$(pwd)/downloads:/app/downloads \\"
  echo "    -v ts_profile:/tmp/ts_profile \\"
  echo "    spotiflac https://open.spotify.com/track/... /app/downloads -s tidal -q LOSSLESS"
  echo
  exec spotiflac --help
fi

exec spotiflac "$@"