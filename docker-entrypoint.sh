#!/usr/bin/env sh
set -e

if [ "$#" -eq 0 ]; then
  echo "SpotiFLAC Docker image: pass a URL and output directory as arguments."
  echo "Example: docker run --rm -v \$(pwd)/downloads:/app/downloads spotiflac \
    https://open.spotify.com/track/... ./downloads -s tidal -q LOSSLESS"
  echo
  exec spotiflac --help
fi

exec spotiflac "$@"
