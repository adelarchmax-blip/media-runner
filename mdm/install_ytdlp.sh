#!/bin/bash
# Installs yt-dlp for MDM the way MDM's own "Install yt-dlp" does, without GitHub's API (whose
# hourly limit shared runner addresses use up): the latest version is read from where
# github.com's releases/latest page leads, its checksum list must carry yt-dlp's signature
# (the key pinned beside this file, the same as MDM's own copy), the program must match its
# checksum, and it is put where MDM looks for it (<data>/tools/yt-dlp/<version>/, current.json).
#
#   install_ytdlp.sh <MDM data dir>
set -euo pipefail
DATA="$1"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="https://github.com/yt-dlp/yt-dlp"
ASSET="yt-dlp_linux"

TAG=$(curl -fsS -o /dev/null -w '%{redirect_url}' "$REPO/releases/latest" | sed -n 's#^.*/releases/tag/##p')
[[ "$TAG" =~ ^[0-9]{4}\.[0-9]{2}\.[0-9]{2}(\.[0-9]+)?$ ]] || { echo "yt-dlp: unexpected latest version '$TAG'" >&2; exit 1; }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
for f in SHA2-256SUMS SHA2-256SUMS.sig "$ASSET"; do
  curl -fsSL --retry 3 -o "$WORK/$f" "$REPO/releases/download/$TAG/$f"
done
gpg --dearmor < "$HERE/yt-dlp-signing-key.asc" > "$WORK/key.gpg"
gpgv --keyring "$WORK/key.gpg" "$WORK/SHA2-256SUMS.sig" "$WORK/SHA2-256SUMS" 2> "$WORK/gpgv.log" || { cat "$WORK/gpgv.log" >&2; echo "yt-dlp: checksum list not signed by yt-dlp's key" >&2; exit 1; }
(cd "$WORK" && grep -E "^[0-9a-f]{64}  $ASSET\$" SHA2-256SUMS | sha256sum -c --quiet -) || { echo "yt-dlp: program doesn't match its signed checksum" >&2; exit 1; }

ROOT="$DATA/tools/yt-dlp"
mkdir -p "$ROOT/$TAG"
install -m 755 "$WORK/$ASSET" "$ROOT/$TAG/$ASSET"
python3 -c 'import json,sys; json.dump({"version": sys.argv[1], "exe": sys.argv[2]}, open(sys.argv[3], "w"), indent=2)' "$TAG" "$ROOT/$TAG/$ASSET" "$ROOT/current.json.tmp"
mv "$ROOT/current.json.tmp" "$ROOT/current.json"
"$ROOT/$TAG/$ASSET" --version > /dev/null
echo "yt-dlp $TAG verified and installed: $ROOT/$TAG/$ASSET"
