# myOwnHyprland

A **100% local** AI agent (Ollama + qwen3.5:9b) that writes, validates and fixes Hyprland configs (Lua, 0.56+),
Waybar configs and **Quickshell** widgets (QML).

> The agent and its interface speak **French**: prompts, answers and UI messages are in French. The French
> quotes below are shown as they appear in the app, with an English gloss.

```
request ─► RAG (Hyprland wiki + Waybar wiki + hl.* stubs) ─► qwen3.5 + tools
                                                              │
        ┌──────────── errors ◄─── Hyprland --verify-config ◄──┤ write_file
        │                     ◄─── waybar (3 s live test)  ◄──┘
        ▼
   fix ─► validate ─► re-read the request ─► workspaces/<date>/ ─► (apply) ~/.config
                                                └─► dataset.jsonl (if you approve the example)
```

## Installation

**Requirements**: Hyprland 0.55+ (Lua config), [Ollama](https://ollama.com) (ideally with ~8 GB of VRAM),
[uv](https://docs.astral.sh/uv), git, lua. Optional: waybar, quickshell (+ qt6-declarative for qmllint),
matugen, gh (logged in, to fetch examples from GitHub).

```bash
git clone <this repo> ~/myOwnHyprland && cd ~/myOwnHyprland
./install.sh           # models, docs for YOUR versions, examples, index, "myhypr" launcher + menu entry
./install.sh --bind    # same + a Hyprland keybind (first free combination in your session, with confirmation)
```

The script is **safe to re-run**: it only redoes what is missing. `./install.sh --update` updates the docs
(Hyprland wiki, Waybar wiki, Quickshell docs for the installed version) and the examples, then reindexes.

The keybind is written to `~/.config/hypr/myhypr.lua` (centred floating window) and hooked in with a single
`require("myhypr")` line, added to `custom.lua` if your config loads it, otherwise to `hyprland.lua`; a
`.bak-myhypr` backup is made and the change is checked with `--verify-config` before it is kept.

`myhypr doctor` checks everything: dependencies, models, model context size, docs matching the installed
versions, up-to-date index, launcher, keybind.

## Usage

### Chat interface (recommended)

```bash
uv run myhypr            # or: alias myhypr='uv run --project ~/delivery/MY/myOwnHyprland myhypr'
```

You chat with the agent in an interactive terminal session: answers are streamed, tool calls are shown as they
happen (`● Write(...)`, `⎿ ✓ config ok`), and the conversation carries over between messages (« maintenant mets
l'horloge à gauche », "now put the clock on the left"). Tab completes commands, Ctrl+C interrupts the agent,
Ctrl+D quits.

| Command | Effect |
|---------|--------|
| `/from hypr waybar` | start from a copy of your current config |
| `/files`, `/show <f>`, `/diff` | see what the agent produced |
| `/apply`, `/restore <backup>` | apply to `~/.config` (diff + confirmation + backup) / roll back |
| `/keep`, `/dataset` | keep the last result for fine-tuning |
| `/new`, `/think`, `/model`, `/search` | new conversation, reasoning on/off, switch model, test the RAG |

### Command line

```bash
uv run myhypr ask "waybar minimaliste, workspaces à gauche, horloge au centre"
uv run myhypr ask "ajoute un blur sur waybar et des animations plus rapides" --from-current hypr waybar
uv run myhypr ask "..." --think            # qwen reasoning enabled (slower, sometimes better)

uv run myhypr search "layer rule blur"     # test what the RAG retrieves
uv run myhypr apply 20261022-112657        # copy into ~/.config (automatic backup in data/backups)
uv run myhypr restore <backup>             # roll back
uv run myhypr dataset                      # fine-tuning dataset progress
```

(The example requests are in French, since that is the agent's language: "minimalist waybar, workspaces on the
left, clock in the centre" and "add blur to waybar and faster animations".)

The agent **always** works in `workspaces/<date>/` (a mirror of `~/.config`). Nothing touches your config
until you run `/apply`, which performs a **safe live apply**:
1. diff + confirmation, then a backup in `data/backups/<date>/` (including the list of created directories);
2. copy into `~/.config` (symlinks into a dotfiles repo are respected);
3. `hyprctl reload` then `hyprctl configerrors`: any **new** error → automatic restore;
4. Waybar is reloaded with `SIGUSR2` if it uses `~/.config/waybar` (otherwise you are warned), and restored if it stops;
5. « Garder ? » ("Keep?"): without an "o" (oui, yes) within 20 s, everything is restored (like display settings
   dialogs — handy if the new config breaks the keyboard or the display).
`/restore <date>` remains available afterwards.

## Migrating hyprland.conf → Lua

Since 0.55, Hyprland is configured in Lua (hyprlock, hypridle and hyprpaper stay on hyprlang).
`myhypr/migrate.py` **deterministically** converts everything that has a safe equivalent: sections,
`$variables` (including `$bg-dir` → `bg_dir`, and variables shared across files), `bind*` and their flags
(`bindel` → `{ repeating, locked }`, `bindd` → description, `bindm`), ~40 dispatchers (`killactive`,
`movetoworkspacesilent` → `follow = false`, `hyprctl dispatch X` inside an exec…), submaps, `monitor`, `exec-once`,
`env`, both `windowrule` syntaxes, `layerrule`, `workspace`, `bezier`/`animation`, `gestures` → `hl.gesture`,
`device`, `source` (one Lua module per file). Everything else becomes `-- TODO migration` with the original line.

```bash
uv run myhypr migrate ~/.config/hypr/hyprland.conf   # no AI → workspaces/<date>/hypr
uv run myhypr   then   /migrate                      # with the agent: conversion + fixes + validation
```

Tested on real configs: JaKooLit/Hyprland-Dots (46 files, 502 lines, 9 TODOs), prasanthrangan/hyprdots
(309 lines, 0 TODOs), linuxmobile/hyprland-dots (216 lines, 2 TODOs). The remaining errors come from options
removed since then (`pseudotile`, `drop_shadow`…), which the agent fixes using the list of valid keys.

## Hypr ecosystem: hyprlock, hypridle, hyprpaper

These stay on hyprlang (`hypr/hyprlock.conf`, `hypridle.conf`, `hyprpaper.conf`). None of them has a
config-check option, and launching them would affect the session (locking, idle, wallpaper), so
`verify_hyprtools` validates them **without running them**: options and types taken from the wiki tables (schema
built automatically, with suggestions for typos), image files, command binaries, old-style `hyprctl dispatch`
(→ suggested `hl.dsp.*` conversion), `listener` without `timeout`, old hyprpaper syntax (`preload`),
date formats in a hyprlock label (→ `$TIME`). System detection reports what is already running
(e.g. `awww-daemon`: the wallpaper is not managed by hyprpaper).

Validation only covers files **modified during the request**: an old unused file in the workspace
does not block another request.

## Wallpaper-based theming (matugen)

- `/palette` (or the `wallpaper_palette` tool): Material 3 palette of the current wallpaper (detected via awww / swww /
  hyprpaper) or of an image, computed with `matugen --dry-run` — nothing is written or restarted.
- **Fixed colours**: the agent reuses those exact colours (border primary → tertiary, surface background,
  on_surface text…).
- **Colours that follow the wallpaper**: the agent adds a template in `matugen/` (`[templates.x]` +
  `{{colors.<role>.default.hex}}`). `verify_matugen` renders the templates in a **sandbox** (`--prefix`,
  hooks stripped: no real file is modified, kitty is not restarted), checks that no `{{…}}` is left, and validates
  the generated Lua with Hyprland and the generated CSS (`@define-color …;`).
- Detection lists the files **generated** by your matugen config: the agent edits the template, never the
  generated file (otherwise matugen would overwrite it on the next wallpaper change).

## Quickshell (QML widgets)

- One config = one `quickshell/<name>/shell.qml` directory in the workspace; after `/apply`, launch it with
  `qs -p ~/.config/quickshell/<name>` (your ml4w config occupies `~/.config/quickshell/shell.qml`, so Quickshell
  ignores `qs -c` config names: use `-p`).
- Docs: `scripts/fetch_quickshell_docs.py` fetches the guide + 138 types for **the installed version** (`qs --version`).
- Validation (`verify_quickshell`): `qmllint` (Qt6) restricted to reliable signals (syntax, misspelled property with
  a suggestion), then a real 4 s `qs -p` launch (load failure, ReferenceError, TypeError…). Targeted hints
  fix common QML mistakes (`centerIn` without `anchors.`, `fontSize` instead of `font.pixelSize`…).
- Examples: the official quickshell-examples, your ml4w config, the caelestia bar (filtered by qmllint).

## Evaluation bench

Before touching the prompt, the docs or the model: measure. `evals/cases.toml` contains 31 fixed requests
(Waybar, Hyprland, Quickshell, modifying an existing config, questions) with deterministic checks.
Lua configs are **executed** against a fake `hl` API (`evals/hl_mock.lua`): we check the meaning
("SUPER+Return launches kitty", `input.kb_layout == "fr"`), not a regex on the text.

```bash
uv run myhypr eval                       # everything (~40 min)
uv run myhypr eval '^wb-'                # Waybar cases only
uv run myhypr eval --tag modify          # existing-config modifications only
uv run myhypr eval -n 3                  # 3 repetitions per case (the model is not deterministic)
uv run myhypr eval --model hypr-agent-ft --label apres-finetune
```

Metrics: **valid** (passes validation), **first try** (with no fixes at all), **compliant** (all checks
pass), time. A full run is shown alongside the difference from the previous full run of the same
model. Results go in `evals/results/` (committed), produced files in `evals/runs/` (not committed).

Adding a case: a `[[case]]` block in `cases.toml` (the check syntax is documented at the top of the file).
To start from an existing config: `seed = "basic"` (→ `evals/fixtures/basic/`).

## The 5 stages

| # | Stage | Status | Where |
|---|-------|--------|-------|
| 1 | Specialised system prompt (Lua `hl.*` syntax) | ✅ | `Modelfile` |
| 2 | RAG over the docs for **your** version | ✅ | `myhypr/rag.py`, `docs/` |
| 3 | Agent + tools + automatic validation + review | ✅ | `myhypr/agent.py`, `myhypr/tools.py` |
| 4 | Dataset collection (examples validated **and** reviewed by you) | 🔄 in progress | `data/dataset.jsonl` |
| 5 | LoRA fine-tuning → GGUF → Ollama | ⏳ once ≥ 300 examples | see below |

### Stage 4 — building the dataset
After each successful `ask`, the tool asks whether you want to keep the example. **Only answer "o" (yes) if you
have checked the result yourself**: validation guarantees correct syntax, not that the request was honoured
(observed example: "violet-cyan" colours requested, cyan-green produced). A dataset of 300 good examples is worth
more than 3000 mediocre ones.

### Real-world config examples (`docs/examples/`)
`uv run python scripts/fetch_examples.py` then `uv run myhypr index` builds a base of examples:
- **ml4w** (your local config): animation, decoration and keybind variants (including AZERTY), Waybar modules and themes;
- **GitHub**: ~45 Lua Hyprland dotfiles found via code search, plus Waybar configs from Alexays, HyDE and JaKooLit.

Strict filter: a Lua file only gets in if it passes `Hyprland --verify-config` on your version (most public
dotfiles still use the old `.conf` syntax, which would make the agent regress). Third-party files are not
committed; `docs/examples/SOURCES.md` lists where they come from. Your own examples: drop them in
`docs/examples/perso/` (committed), then run `myhypr index`.

For each request, the agent receives 3 doc passages + 2 real examples. `MYHYPR_NO_EXAMPLES=1` disables the
examples (to measure their contribution with the bench).

### Candidates from the bench
`uv run myhypr candidates` then, in the interface, `/candidates`: the **compliant** outputs from every bench run,
**re-scored with the current checks** (older scores may have been skewed by checks that have since been
fixed) and re-validated, deduplicated, at most 3 per request, cleanest first (first try, few calls). For each
one: the request, the files (or the diff for a modification), the answer; `o` keep, `n` reject, `v` view all,
`s` skip, `q` quit (the prompt itself is in French). Decisions are remembered
(`data/candidates.json`). Each kept example contains the **full trajectory** (tool calls) in addition to
"request → files". `/dataset list` and `/dataset remove N` to clean up.

### Variants: more diversity for the dataset
```bash
uv run myhypr variants generate --paraphrases 3   # → evals/variants/generated.json
uv run myhypr eval --variants                      # runs them (≈ 2 h 30); never counted as a baseline
uv run myhypr   then   /candidates                 # compliant outputs show up in the review
```
- **Parameterised templates** (`evals/variants/templates.toml`): the same request with other values (gaps, keyboard,
  key → application, rules, decoration, bar position, idle timeouts, modifications…), with adjusted checks.
- **Paraphrases** by the base model (casual, polite, English, text-speak). Strict filter: rejected if a number,
  a path (exact case), a colour, a key combination or a checked term disappears, or if the sentence
  is phrased as if the agent were the one speaking.

### Stage 5 — fine-tuning (later)
- Tool: [Unsloth](https://github.com/unslothai/unsloth), 4-bit QLoRA on the Qwen3.5-9B base model (HF).
- VRAM: your RTX 5070 Laptop has 8 GB → just enough for a 9B; if it doesn't fit, Colab/Kaggle (free 16 GB T4).
- Format: `data/dataset.jsonl` is already in the `messages` format (user → assistant) expected by Unsloth/TRL.
- Export: `model.save_pretrained_gguf(..., quantization_method="q4_k_m")`, then replace `FROM` in the
  Modelfile with the `.gguf` and run `ollama create hypr-agent-ft -f Modelfile`.
- Evaluation: rerun the same requests with `--model hypr-agent` and `--model hypr-agent-ft`, and compare
  the first-try validation rate and compliance with the requests.

## Structure

```
Modelfile            system prompt + model parameters
myhypr/rag.py        chunking + embeddings (nomic-embed-text) + cosine search (numpy)
myhypr/tools.py      agent tools + Hyprland / Waybar validators
myhypr/agent.py      generate → validate → fix → review loop
myhypr/dataset.py    successful sessions → dataset.jsonl
myhypr/tui.py        chat interface (rich + prompt_toolkit)
myhypr/ops.py        apply / restore / diff on ~/.config
myhypr/cli.py        commands
docs/raw/            cloned wikis (not committed); docs/examples/ = your reference configs
data/                index, sessions (full traces), backups, dataset
workspaces/          agent outputs, one per request
```
