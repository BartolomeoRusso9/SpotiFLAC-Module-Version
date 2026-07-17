#!/usr/bin/env sh
set -e

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