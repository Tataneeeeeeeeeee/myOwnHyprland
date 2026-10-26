#!/usr/bin/env bash
# myOwnHyprland — installation / mise à jour. Relançable sans risque (idempotent).
#
#   ./install.sh            installe ce qui manque (modèles, docs, exemples, index, lanceur)
#   ./install.sh --update   met aussi à jour la doc et les exemples, puis réindexe
#   ./install.sh --bind     ajoute en plus un raccourci Hyprland (première combinaison libre, avec confirmation)
#   ./install.sh --yes      sans questions
set -euo pipefail
cd "$(dirname "$0")"

UPDATE=0; BIND=0; YES=""
for arg in "$@"; do
    case "$arg" in
        --update) UPDATE=1 ;;
        --bind) BIND=1 ;;
        --yes|-y) YES="--yes" ;;
        -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
        *) echo "option inconnue : $arg"; exit 2 ;;
    esac
done

step() { printf '\n\033[1;35m◆ %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '  \033[31m✗ %s\033[0m\n' "$*"; exit 1; }

step "Prérequis"
need() { command -v "$1" >/dev/null 2>&1 && ok "$1" || die "$1 introuvable — $2"; }
need ollama "https://ollama.com (Arch : pacman -S ollama, puis systemctl enable --now ollama)"
need uv "https://docs.astral.sh/uv (Arch : pacman -S uv)"
need git "pacman -S git"
need Hyprland "myOwnHyprland configure Hyprland 0.55+"
need lua "pacman -S lua (utilisé pour analyser les configs Lua)"
for opt in waybar qs matugen gh; do
    command -v "$opt" >/dev/null 2>&1 && ok "$opt" || warn "$opt absent (optionnel)"
done
ollama list >/dev/null 2>&1 || die "le serveur Ollama ne répond pas : systemctl --user start ollama (ou ollama serve)"

step "Modèles Ollama"
for m in qwen3.5:9b nomic-embed-text; do
    if ollama list | awk '{print $1}' | grep -qx "$m\(:latest\)\?"; then ok "$m déjà présent"; else ollama pull "$m"; fi
done

step "Environnement Python"
uv sync -q && ok "dépendances installées"

step "Modèle spécialisé hypr-agent"
ollama create hypr-agent -f Modelfile >/dev/null && ok "hypr-agent (re)créé depuis le Modelfile"

step "Documentation"
if [[ $UPDATE == 1 || ! -d docs/raw/hyprland-wiki ]]; then
    mkdir -p docs/raw && ./scripts/fetch_docs.sh
else
    ok "déjà présente (--update pour la mettre à jour)"
fi

step "Exemples de configs réelles"
if [[ $UPDATE == 1 || ! -d docs/examples/github ]]; then
    command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1 || warn "gh absent ou non connecté : seulement tes configs locales"
    uv run python scripts/fetch_examples.py | tail -1
else
    ok "déjà présents (--update pour les rafraîchir)"
fi

step "Index de recherche"
if [[ $UPDATE == 1 || ! -f data/index/chunks.json ]] || [[ -n "$(find docs -name '*.md' -newer data/index/chunks.json -print -quit)" ]]; then
    uv run myhypr index
else
    ok "à jour"
fi

step "Intégration au bureau"
if [[ $BIND == 1 ]]; then uv run myhypr setup --bind $YES; else uv run myhypr setup; fi

step "Diagnostic"
uv run myhypr doctor || true
printf '\n\033[1;35m◆ Prêt.\033[0m Lance \033[1mmyhypr\033[0m (ou ton raccourci) — /help pour les commandes.\n'
