#!/usr/bin/env bash
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dest="${1:-$here/../organon_upstream}"
ref=fcd1e501ee589d363766136aceb60527a0f06b12

if [ -e "$dest" ]; then
  echo "Destination already exists: $dest" >&2
  exit 1
fi

git init --quiet "$dest"
git -C "$dest" remote add origin https://github.com/cheikh025/Organon.git
git -C "$dest" fetch --quiet --depth 1 origin "$ref"
git -C "$dest" switch --quiet --detach FETCH_HEAD
test "$(git -C "$dest" rev-parse HEAD)" = "$ref"
git -C "$dest" apply --check "$here/organon-v1-v2.patch"
git -C "$dest" apply "$here/organon-v1-v2.patch"
printf 'Organon V1/V2 checkout: %s\n' "$dest"
