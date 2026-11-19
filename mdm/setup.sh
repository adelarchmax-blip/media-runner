#!/bin/bash
# Installs Mister Download Manager and its official Chromium extension from the project's own
# GitHub release, checked against the release's SHA256SUMS. MDM fetches its own signed yt-dlp
# helper for video pages (its "Install yt-dlp" step).
set -euo pipefail
VER="${MDM_VERSION:-0.5.2}"
HOME_DIR="${MDM_HOME:-$HOME/mdm}"
BASE="https://github.com/anefzaoui/misterdownloadmanager/releases/download/v${VER}"
DEB="MisterDownloadManager-${VER}-linux-x86_64.deb"
EXT="MisterDownloadManager-${VER}-extension-chromium.zip"

mkdir -p "$HOME_DIR/pkg" "$HOME_DIR/extension" "$HOME_DIR/data"
cd "$HOME_DIR/pkg"
curl -fsSL -o SHA256SUMS.txt "$BASE/SHA256SUMS.txt"
curl -fsSL -o "$DEB" "$BASE/$DEB"
curl -fsSL -o "$EXT" "$BASE/$EXT"
grep -E " \*?(${DEB}|${EXT})$" SHA256SUMS.txt | sed 's/ \*/  /' | sha256sum -c -

sudo apt-get install -y -qq "./$DEB" > /dev/null
unzip -q -o "$EXT" -d "$HOME_DIR/extension"
MANIFEST=$(find "$HOME_DIR/extension" -name manifest.json -print -quit)
echo "MDM_EXTENSION=$(dirname "$MANIFEST")" >> "${GITHUB_ENV:-/dev/null}"

# GitHub's API limits shared runner addresses: a few tries, further apart each time.
for i in 1 2 3 4 5; do
  MDM_DATA_DIR="$HOME_DIR/data" misterdownloadmanager-cli install-ytdlp && break
  [ "$i" = 5 ] && exit 1
  sleep $((i * 20))
done
echo "MDM $VER ready"
