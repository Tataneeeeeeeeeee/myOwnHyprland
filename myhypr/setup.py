"""Intégration au bureau et diagnostic : `myhypr setup` et `myhypr doctor`.

setup :
- lanceur ~/.local/bin/myhypr (utilisable de partout) ;
- entrée de menu ~/.local/share/applications/myhypr.desktop (rofi, walker, fuzzel…) ;
- (optionnel, avec confirmation) raccourci Hyprland vers une fenêtre flottante, sur la PREMIÈRE combinaison libre
  de ta session, dans ~/.config/hypr/myhypr.lua chargé par une ligne `require("myhypr")`.
doctor : vérifie dépendances, modèles, docs (et qu'elles correspondent aux versions installées), index, intégration.
"""

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

from .config import CHAT_MODEL, CONFIG_HOME, DATA, DOCS_RAW, EMBED_MODEL, INDEX_DIR, NUM_CTX, ROOT

LAUNCHER = Path.home() / ".local/bin/myhypr"
DESKTOP = Path.home() / ".local/share/applications/myhypr.desktop"
SNIPPET = CONFIG_HOME / "hypr" / "myhypr.lua"
COMBOS = ["SUPER + CTRL + A", "SUPER + ALT + A", "SUPER + ALT + H", "SUPER + CTRL + H", "SUPER + F1",
          "SUPER + ALT + Y", "SUPER + ALT + RETURN"]
# terminal → commande qui ouvre une fenêtre de classe « myhypr » (pour la règle de fenêtre flottante)
TERMINALS = {"kitty": "kitty --class myhypr -e", "foot": "foot --app-id myhypr", "alacritty": "alacritty --class myhypr -e",
             "wezterm": "wezterm start --class myhypr --", "ghostty": "ghostty --class=myhypr -e",
             "konsole": "konsole -e"}


def run(cmd: list[str], timeout: int = 10) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def terminal_cmd() -> str | None:
    return next((cmd for term, cmd in TERMINALS.items() if shutil.which(term)), None)


# --------------------------------------------------------------------------- setup

def install_launcher() -> str:
    LAUNCHER.parent.mkdir(parents=True, exist_ok=True)
    LAUNCHER.write_text(f'#!/usr/bin/env sh\n# myOwnHyprland — généré par install.sh\n'
                        f'exec uv run --project "{ROOT}" --quiet myhypr "$@"\n')
    LAUNCHER.chmod(0o755)
    term = terminal_cmd()
    DESKTOP.parent.mkdir(parents=True, exist_ok=True)
    DESKTOP.write_text("[Desktop Entry]\nType=Application\nName=myOwnHyprland\n"
                       "Comment=Agent IA local pour configurer Hyprland, Waybar et Quickshell\n"
                       f"Exec={term + ' ' if term else ''}{LAUNCHER}\nTerminal={'false' if term else 'true'}\n"
                       "Icon=utilities-terminal\nCategories=Settings;System;\n")
    return f"lanceur {LAUNCHER} · menu {DESKTOP}"


def free_combo() -> str | None:
    from .binds import from_config, from_session, normalize
    used = {b.combo for b in from_session() if not b.submap}
    if not used:  # Hyprland ne tourne pas : on lit la config
        entry = (CONFIG_HOME / "hypr" / "hyprland.lua").resolve()
        used = {b.combo for b in from_config(entry) if not b.submap}
    return next((c for c in COMBOS if normalize(c) not in used), None)


def hook_file() -> Path:
    """Fichier où ajouter require("myhypr") : custom.lua si la config le charge déjà, sinon hyprland.lua."""
    main = (CONFIG_HOME / "hypr" / "hyprland.lua").resolve()
    custom = CONFIG_HOME / "hypr" / "custom.lua"
    if custom.exists() and re.search(r"require\(\s*[\"']custom[\"']\s*\)", main.read_text(errors="ignore")):
        return custom.resolve()
    return main


def install_bind(confirm) -> str:
    from .tools import verify_hyprland_file
    combo, term = free_combo(), terminal_cmd()
    if not combo:
        return "aucune combinaison libre parmi " + ", ".join(COMBOS) + " : raccourci non installé"
    if not term:
        return "aucun terminal connu (kitty, foot, alacritty…) : raccourci non installé"
    SNIPPET.write_text(
        "-- myOwnHyprland : ouvre l'agent dans une fenêtre flottante (généré par `myhypr setup --bind`)\n"
        f'hl.bind("{combo}", hl.dsp.exec_cmd("{term} {LAUNCHER}"), {{ description = "myOwnHyprland (agent IA)" }})\n'
        'hl.window_rule({ name = "myhypr-float", match = { class = "^(myhypr)$" }, float = true, '
        'size = "1100 760", center = true })\n')
    verdict = verify_hyprland_file(SNIPPET.resolve())
    if verdict != "config ok":
        SNIPPET.unlink()
        return f"le fichier généré ne passe pas la validation ({verdict.splitlines()[0]}) : annulé"
    hook = hook_file()
    text = hook.read_text(errors="ignore")
    if re.search(r"require\(\s*[\"']myhypr[\"']\s*\)", text):
        return f"raccourci {combo} déjà branché ({hook.name})"
    if not confirm(f"Ajouter require(\"myhypr\") à la fin de {hook} (sauvegarde .bak-myhypr) pour activer {combo} ?"):
        return f"raccourci prêt dans {SNIPPET} mais non branché (ajoute require(\"myhypr\") toi-même)"
    shutil.copy2(hook, hook.with_name(hook.name + ".bak-myhypr"))
    hook.write_text(text.rstrip("\n") + '\n\n-- myOwnHyprland\nrequire("myhypr")\n')
    entry = (CONFIG_HOME / "hypr" / "hyprland.lua").resolve()
    verdict = verify_hyprland_file(entry)
    if verdict != "config ok":
        shutil.copy2(hook.with_name(hook.name + ".bak-myhypr"), hook)
        return f"la config ne valide plus après ajout ({verdict.splitlines()[0]}) : {hook.name} restauré"
    return f"raccourci {combo} → fenêtre flottante myOwnHyprland (Hyprland recharge sa config tout seul)"


# --------------------------------------------------------------------------- doctor

def doctor() -> list[tuple[str, str, str]]:
    """[(niveau ok|warn|err, élément, détail)]"""
    out: list[tuple[str, str, str]] = []

    def add(level, item, detail=""):
        out.append((level, item, detail))

    for b, needed in (("ollama", True), ("uv", True), ("git", True), ("Hyprland", True), ("lua", True),
                      ("waybar", False), ("qs", False), ("matugen", False), ("gh", False)):
        if shutil.which(b):
            add("ok", b)
        else:
            add("err" if needed else "warn", b, "requis" if needed else "optionnel : fonctionnalités en moins")
    qmllint = Path("/usr/lib/qt6/bin/qmllint")
    add("ok" if qmllint.exists() else "warn", "qmllint (Qt6)", "" if qmllint.exists() else "validation QML réduite")

    r = run(["ollama", "list"])
    models = r.stdout if r and r.returncode == 0 else ""
    if not r or r.returncode != 0:
        add("err", "serveur Ollama", "injoignable : lance « ollama serve » ou le service")
    for m in ("qwen3.5:9b", EMBED_MODEL, CHAT_MODEL):
        add("ok" if m in models else "err", f"modèle {m}", "" if m in models else
            f"absent : {'ollama create hypr-agent -f Modelfile' if m == CHAT_MODEL else 'ollama pull ' + m}")
    r = run(["ollama", "show", CHAT_MODEL, "--parameters"])
    m = re.search(r"num_ctx\s+(\d+)", r.stdout) if r and r.returncode == 0 else None
    if m and int(m.group(1)) != NUM_CTX:
        add("warn", "contexte du modèle", f"{m.group(1)} ≠ NUM_CTX {NUM_CTX} : recrée le modèle (ollama create …)")
    elif m:
        add("ok", "contexte du modèle", f"{m.group(1)} tokens")

    v = run(["hyprctl", "version", "-j"])
    try:
        version = json.loads(v.stdout).get("version", "?") if v and v.returncode == 0 else None
    except json.JSONDecodeError:
        version = None
    if version:
        major_minor = tuple(int(x) for x in version.split(".")[:2])
        add("ok" if major_minor >= (0, 55) else "err", f"Hyprland {version}",
            "" if major_minor >= (0, 55) else "config Lua requise (0.55+)")
    fmt = "lua" if (CONFIG_HOME / "hypr/hyprland.lua").exists() else \
        "conf" if (CONFIG_HOME / "hypr/hyprland.conf").exists() else None
    add("ok" if fmt == "lua" else "warn", "format de config", {"lua": "Lua", "conf": "hyprlang : /migrate pour passer en Lua",
                                                                 None: "aucune config trouvée"}[fmt])

    wiki = DOCS_RAW / "hyprland-wiki"
    if wiki.exists():
        r = run(["git", "-C", str(wiki), "log", "-1", "--format=%cd", "--date=short"])
        add("ok", "wiki Hyprland", f"du {r.stdout.strip() if r else '?'} (mise à jour : ./install.sh --update)")
    else:
        add("err", "wiki Hyprland", "absent : ./install.sh")
    qs_ver = (DOCS_RAW / "quickshell" / "VERSION")
    if shutil.which("qs"):
        q = run(["qs", "--version"])
        installed = re.search(r"Quickshell (\d+\.\d+\.\d+)", q.stdout if q else "")
        doc = qs_ver.read_text().strip() if qs_ver.exists() else None
        if installed and doc != installed.group(1):
            add("warn", "doc Quickshell", f"pour {doc or 'aucune version'} mais Quickshell {installed.group(1)} installé "
                                          "→ ./install.sh --update")
        elif installed:
            add("ok", "doc Quickshell", f"v{doc}")
    chunks = INDEX_DIR / "chunks.json"
    if chunks.exists():
        newest_doc = max((p.stat().st_mtime for p in DOCS_RAW.rglob("*.md")), default=0)
        stale = newest_doc > chunks.stat().st_mtime
        add("warn" if stale else "ok", "index RAG",
            f"{len(json.loads(chunks.read_text()))} passages" + (" — plus ancien que la doc : myhypr index" if stale else ""))
    else:
        add("err", "index RAG", "absent : uv run myhypr index")

    from .dataset import rows
    add("ok", "dataset", f"{len(rows())} exemple(s)")
    add("ok" if LAUNCHER.exists() else "warn", "lanceur", str(LAUNCHER) if LAUNCHER.exists() else "myhypr setup")
    on_path = shutil.which("myhypr") is not None
    if LAUNCHER.exists() and not on_path:
        add("warn", "PATH", "~/.local/bin n'est pas dans le PATH")
    add("ok" if SNIPPET.exists() else "warn", "raccourci Hyprland",
        SNIPPET.read_text().split('hl.bind("')[1].split('"')[0] if SNIPPET.exists() else "myhypr setup --bind")
    return out
