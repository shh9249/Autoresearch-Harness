#!/usr/bin/env bash
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dest="${1:-$here/../organon_upstream}"
ref=fcd1e501ee589d363766136aceb60527a0f06b12
[[ ! -e "$dest" ]] || { echo "Choose a fresh checkout: $dest already exists" >&2; exit 1; }
git init --quiet "$dest"
git -C "$dest" remote add origin https://github.com/cheikh025/Organon.git
git -C "$dest" fetch --quiet --depth 1 origin "$ref"
git -C "$dest" checkout --quiet --detach FETCH_HEAD
test "$(git -C "$dest" rev-parse HEAD)" = "$ref"
git -C "$dest" apply --check "$here/native-search.patch"
git -C "$dest" apply "$here/native-search.patch"
# Author setup installs dist/ tarballs. Repack just the changed JS plugin.
(cd "$dest/plugins/dsh-evolve-loop" && npm pack --quiet --ignore-scripts --pack-destination ../../dist)
echo "Next: cd '$dest' and run bash setup.sh."
