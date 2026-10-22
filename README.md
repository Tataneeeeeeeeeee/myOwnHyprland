# myOwnHyprland

Agent IA **100 % local** (Ollama + qwen3.5:9b) qui écrit, valide et corrige des configs Hyprland (Lua, 0.56+) et Waybar.

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
tant que tu ne lances pas `apply`. Note : `verify_waybar` affiche une waybar de test pendant 3 s.

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

Pour améliorer le RAG sans entraîner : dépose des configs que tu aimes dans `docs/examples/` puis relance
`myhypr index`.

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
