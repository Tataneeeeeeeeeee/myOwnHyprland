#!/usr/bin/env bash
# Clones or updates the wikis used by the RAG, then reindexes.
set -euo pipefail
cd "$(dirname "$0")/../docs/raw"

sync_repo() {
    if [[ -d $2/.git ]]; then git -C "$2" pull -q --ff-only; else git clone -q --depth 1 "$1" "$2"; fi
    echo "✓ $2"
}
sync_repo https://github.com/hyprwm/hyprland-wiki hyprland-wiki
sync_repo https://github.com/Alexays/Waybar.wiki.git waybar-wiki

cd ../..
if command -v qs >/dev/null 2>&1; then uv run python scripts/fetch_quickshell_docs.py; else echo "· Quickshell absent : doc Quickshell ignorée"; fi
echo "Pense à relancer : uv run myhypr index"
