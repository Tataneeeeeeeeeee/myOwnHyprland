# myOwnHyprland

Agent IA **100 % local** (Ollama + qwen3.5:9b) qui écrit, valide et corrige des configs Hyprland (Lua, 0.56+),
Waybar et des widgets **Quickshell** (QML).

```
demande ─► RAG (wiki Hyprland + wiki Waybar + stubs hl.*) ─► qwen3.5 + outils
                                                              │
        ┌──────────── erreurs ◄── Hyprland --verify-config ◄──┤ write_file
        │                       ◄── waybar (test live 3 s) ◄──┘
        ▼
   corrige ─► valide ─► relecture de la demande ─► workspaces/<date>/ ─► (apply) ~/.config
                                                     └─► dataset.jsonl (si tu valides l'exemple)
```

## Installation

```bash
ollama pull qwen3.5:9b && ollama pull nomic-embed-text
ollama create hypr-agent -f Modelfile      # étape 1 : prompt système spécialisé
./scripts/fetch_docs.sh                     # clone/maj des wikis dans docs/raw
uv sync
uv run myhypr index                         # étape 2 : indexation RAG (~1 min)
```

## Utilisation

### Interface conversationnelle (recommandé)

```bash
uv run myhypr            # ou : alias myhypr='uv run --project ~/delivery/MY/myOwnHyprland myhypr'
```

On discute avec l'agent comme dans Claude Code : streaming des réponses, appels d'outils visibles
(`● Write(...)`, `⎿ ✓ config ok`), la conversation est gardée entre les messages (« maintenant mets l'horloge
à gauche »). Tab complète les commandes, Ctrl+C interrompt l'agent, Ctrl+D quitte.

| Commande | Effet |
|----------|-------|
| `/from hypr waybar` | part d'une copie de ta config actuelle |
| `/files`, `/show <f>`, `/diff` | voir ce que l'agent a produit |
| `/apply`, `/restore <backup>` | appliquer dans `~/.config` (diff + confirmation + sauvegarde) / revenir en arrière |
| `/keep`, `/dataset` | garder le dernier résultat pour le fine-tuning |
| `/new`, `/think`, `/model`, `/search` | nouvelle conversation, raisonnement on/off, changer de modèle, tester le RAG |

### En ligne de commande

```bash
uv run myhypr ask "waybar minimaliste, workspaces à gauche, horloge au centre"
uv run myhypr ask "ajoute un blur sur waybar et des animations plus rapides" --from-current hypr waybar
uv run myhypr ask "..." --think            # raisonnement qwen activé (plus lent, parfois meilleur)

uv run myhypr search "layer rule blur"     # tester ce que le RAG retrouve
uv run myhypr apply 20261022-112657        # copie dans ~/.config (sauvegarde auto dans data/backups)
uv run myhypr restore <backup>             # retour arrière
uv run myhypr dataset                      # avancement du dataset de fine-tuning
```

L'agent travaille **toujours** dans `workspaces/<date>/` (miroir de `~/.config`). Rien ne touche ta config
tant que tu ne lances pas `/apply`, qui fait une **application à chaud sécurisée** :
1. diff + confirmation, puis sauvegarde dans `data/backups/<date>/` (y compris la liste des dossiers créés) ;
2. copie dans `~/.config` (les liens symboliques vers un dépôt de dotfiles sont respectés) ;
3. `hyprctl reload` puis `hyprctl configerrors` : toute **nouvelle** erreur → restauration automatique ;
4. Waybar rechargée par `SIGUSR2` si elle utilise `~/.config/waybar` (sinon tu es prévenu), et restauration si elle s'arrête ;
5. « Garder ? » : sans « o » dans les 20 s, tout est restauré (comme les réglages d'écran — utile si la
   nouvelle config casse le clavier ou l'affichage).
`/restore <date>` reste disponible ensuite.

## Migration hyprland.conf → Lua

Depuis la 0.55, Hyprland se configure en Lua (hyprlock, hypridle, hyprpaper restent en hyprlang).
`myhypr/migrate.py` convertit de façon **déterministe** tout ce qui a une correspondance sûre : sections,
`$variables` (y compris `$bg-dir` → `bg_dir`, variables partagées entre fichiers), `bind*` et leurs drapeaux
(`bindel` → `{ repeating, locked }`, `bindd` → description, `bindm`), ~40 dispatchers (`killactive`,
`movetoworkspacesilent` → `follow = false`, `hyprctl dispatch X` dans un exec…), sous-modes, `monitor`, `exec-once`,
`env`, les deux syntaxes de `windowrule`, `layerrule`, `workspace`, `bezier`/`animation`, `gestures` → `hl.gesture`,
`device`, `source` (un module Lua par fichier). Le reste devient `-- TODO migration` avec la ligne d'origine.

```bash
uv run myhypr migrate ~/.config/hypr/hyprland.conf   # sans IA → workspaces/<date>/hypr
uv run myhypr   puis   /migrate                       # avec l'agent : conversion + correction + validation
```

Testé sur de vraies configs : JaKooLit/Hyprland-Dots (46 fichiers, 502 lignes, 9 TODO), prasanthrangan/hyprdots
(309 lignes, 0 TODO), linuxmobile/hyprland-dots (216 lignes, 2 TODO). Les erreurs restantes viennent d'options
supprimées depuis (`pseudotile`, `drop_shadow`…), que l'agent corrige avec la liste des clés valides.

## Écosystème Hypr : hyprlock, hypridle, hyprpaper

Ils restent en hyprlang (`hypr/hyprlock.conf`, `hypridle.conf`, `hyprpaper.conf`). Aucun n'a d'option de
vérification et les lancer agirait sur la session (verrouillage, veille, fond) : `verify_hyprtools` les valide
**sans les exécuter** — options et types d'après les tableaux du wiki (schéma construit automatiquement, avec
suggestion en cas de faute), fichiers d'images, binaires des commandes, `hyprctl dispatch` à l'ancienne
(→ conversion `hl.dsp.*` proposée), `listener` sans `timeout`, ancienne syntaxe de hyprpaper (`preload`),
formats de date dans un label hyprlock (→ `$TIME`). La détection du système indique ce qui tourne déjà
(ex. `awww-daemon` : le fond d'écran n'est pas géré par hyprpaper).

La validation ne porte que sur les fichiers **modifiés pendant la demande** : un vieux fichier inutilisé du
dossier de travail ne bloque pas une autre demande.

## Thème tiré du fond d'écran (matugen)

- `/palette` (ou l'outil `wallpaper_palette`) : palette Material 3 du fond affiché (détecté via awww / swww /
  hyprpaper) ou d'une image, calculée par `matugen --dry-run` — rien n'est écrit ni relancé.
- **Couleurs fixes** : l'agent reprend ces couleurs exactes (bordure primary → tertiary, fond surface, texte
  on_surface…).
- **Couleurs qui suivent le fond** : l'agent ajoute un template dans `matugen/` (`[templates.x]` +
  `{{colors.<rôle>.default.hex}}`). `verify_matugen` rend les templates dans un **bac à sable** (`--prefix`,
  hooks retirés : aucun fichier réel modifié, kitty pas relancé), vérifie qu'aucun `{{…}}` ne reste, valide le
  Lua généré avec Hyprland et le CSS généré (`@define-color …;`).
- La détection liste les fichiers **générés** par ta config matugen : l'agent modifie le template, jamais le
  fichier généré (sinon matugen l'écraserait au prochain changement de fond).

## Quickshell (widgets QML)

- Une config = un dossier `quickshell/<nom>/shell.qml` dans le dossier de travail ; après `/apply`, lance-la avec
  `qs -p ~/.config/quickshell/<nom>` (ta config ml4w occupe `~/.config/quickshell/shell.qml`, donc les noms de
  config `qs -c` sont ignorés par Quickshell : passe par `-p`).
- Doc : `scripts/fetch_quickshell_docs.py` récupère guide + 138 types de **la version installée** (`qs --version`).
- Validation (`verify_quickshell`) : `qmllint` (Qt6) limité aux signaux fiables (syntaxe, faute de propriété avec
  suggestion), puis lancement réel `qs -p` de 4 s (échec de chargement, ReferenceError, TypeError…). Des indices
  ciblés corrigent les erreurs QML fréquentes (`centerIn` sans `anchors.`, `fontSize` au lieu de `font.pixelSize`…).
- Exemples : exemples officiels quickshell-examples, ta config ml4w, la barre de caelestia (filtrés par qmllint).

## Banc d'évaluation

Avant de toucher au prompt, à la doc ou au modèle : mesurer. `evals/cases.toml` contient 31 demandes fixes
(Waybar, Hyprland, Quickshell, modification d'une config existante, question) avec des vérifications déterministes.
Les configs Lua sont **exécutées** avec une fausse API `hl` (`evals/hl_mock.lua`) : on vérifie le sens
(« SUPER+Return lance kitty », `input.kb_layout == "fr"`), pas une regex sur le texte.

```bash
uv run myhypr eval                       # tout (~40 min)
uv run myhypr eval '^wb-'                # seulement les cas Waybar
uv run myhypr eval --tag modify          # seulement les modifications de config existante
uv run myhypr eval -n 3                  # 3 répétitions par cas (le modèle n'est pas déterministe)
uv run myhypr eval --model hypr-agent-ft --label apres-finetune
```

Mesures : **valide** (passe la validation), **1er coup** (sans aucune correction), **conforme** (toutes les
vérifications OK), temps. Un run complet s'affiche avec l'écart par rapport au précédent run complet du même
modèle. Résultats dans `evals/results/` (versionnés), fichiers produits dans `evals/runs/` (non versionnés).

Ajouter un cas : un bloc `[[case]]` dans `cases.toml` (la syntaxe des vérifications est en tête de fichier).
Pour partir d'une config existante : `seed = "basic"` (→ `evals/fixtures/basic/`).

## Les 5 étapes

| # | Étape | État | Où |
|---|-------|------|----|
| 1 | Prompt système spécialisé (syntaxe Lua `hl.*`) | ✅ | `Modelfile` |
| 2 | RAG sur la doc de **ta** version | ✅ | `myhypr/rag.py`, `docs/` |
| 3 | Agent + outils + validation auto + relecture | ✅ | `myhypr/agent.py`, `myhypr/tools.py` |
| 4 | Collecte du dataset (exemples validés **et** relus par toi) | 🔄 en cours | `data/dataset.jsonl` |
| 5 | Fine-tuning LoRA → GGUF → Ollama | ⏳ quand ≥ 300 exemples | voir plus bas |

### Étape 4 — construire le dataset
Après chaque `ask` réussi, l'outil demande si tu gardes l'exemple. **Ne réponds « o » que si tu as vérifié
le résultat toi-même** : la validation garantit une syntaxe correcte, pas que la demande soit respectée
(ex. observé : couleurs « violet-cyan » demandées, cyan-vert produites). Un dataset de 300 bons exemples vaut
mieux que 3000 moyens.

### Exemples de configs réelles (`docs/examples/`)
`uv run python scripts/fetch_examples.py` puis `uv run myhypr index` construit une base d'exemples :
- **ml4w** (ta config locale) : variantes d'animations, décorations, raccourcis (avec AZERTY), modules et thèmes Waybar ;
- **GitHub** : ~45 dotfiles Hyprland en Lua trouvés via la recherche de code, plus Waybar d'Alexays, HyDE et JaKooLit.

Filtre strict : un Lua n'entre que s'il passe `Hyprland --verify-config` sur ta version (la plupart des
dotfiles publics sont encore en ancienne syntaxe `.conf`, qui ferait régresser l'agent). Les fichiers tiers ne
sont pas versionnés ; `docs/examples/SOURCES.md` liste les origines. Tes propres exemples : dépose-les dans
`docs/examples/perso/` (versionné) puis `myhypr index`.

À chaque demande, l'agent reçoit 3 passages de doc + 2 exemples réels. `MYHYPR_NO_EXAMPLES=1` désactive les
exemples (pour mesurer leur apport avec le banc).

### Étape 5 — fine-tuning (plus tard)
- Outil : [Unsloth](https://github.com/unslothai/unsloth), QLoRA 4 bits sur le modèle de base Qwen3.5-9B (HF).
- VRAM : ta RTX 5070 Laptop a 8 Go → juste pour un 9B ; si ça ne passe pas, Colab/Kaggle (T4 16 Go gratuit).
- Format : `data/dataset.jsonl` est déjà au format `messages` (user → assistant) attendu par Unsloth/TRL.
- Export : `model.save_pretrained_gguf(..., quantization_method="q4_k_m")`, puis remplacer `FROM` dans le
  Modelfile par le `.gguf` et `ollama create hypr-agent-ft -f Modelfile`.
- Évaluation : relancer les mêmes demandes avec `--model hypr-agent` et `--model hypr-agent-ft`, comparer
  le taux de validation au premier essai et la conformité aux demandes.

## Structure

```
Modelfile            prompt système + paramètres du modèle
myhypr/rag.py        découpage + embeddings (nomic-embed-text) + recherche cosinus (numpy)
myhypr/tools.py      outils de l'agent + validateurs Hyprland / Waybar
myhypr/agent.py      boucle générer → valider → corriger → relire
myhypr/dataset.py    sessions réussies → dataset.jsonl
myhypr/tui.py        interface conversationnelle (rich + prompt_toolkit)
myhypr/ops.py        apply / restore / diff sur ~/.config
myhypr/cli.py        commandes
docs/raw/            wikis clonés (non versionnés) ; docs/examples/ = tes configs de référence
data/                index, sessions (traces complètes), backups, dataset
workspaces/          sorties de l'agent, une par demande
```
