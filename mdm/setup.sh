#!/bin/bash
# Installs Mister Download Manager and its official Chromium extension from the project's own
# GitHub release, checked against the release's SHA256SUMS. MDM fetches its own signed yt-dlp
# helper for video pages (its "Install yt-dlp" step, or install_ytdlp.sh when GitHub's API refuses).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
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

# yt-dlp: MDM's own install first; GitHub's API limits shared runner addresses, so when it
# refuses, the same verified install without the API (install_ytdlp.sh). yt-dlp serves watch
# pages only, so a torrents-only run goes on without it.
if ! MDM_DATA_DIR="$HOME_DIR/data" misterdownloadmanager-cli install-ytdlp \
  && ! bash "$HERE/install_ytdlp.sh" "$HOME_DIR/data"; then
  [ "${LANES:-all}" = torrents ] || exit 1
  echo "::warning::yt-dlp helper not installed; torrents-only run goes on without it"
fi
echo "MDM $VER ready"
