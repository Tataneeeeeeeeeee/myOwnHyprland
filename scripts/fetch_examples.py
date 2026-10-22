"""Construit docs/examples/ : configs réelles servant d'exemples au RAG.

Sources : ta config ml4w locale + dotfiles publics sur GitHub (via `gh`).
Règle d'or : un fichier Lua n'entre que s'il passe `Hyprland --verify-config` sur TA version
(pas d'ancienne syntaxe hyprlang, pas de fonction inventée). Les JSON Waybar doivent se parser.

Usage : uv run python scripts/fetch_examples.py
"""

import hashlib
import json
import re
import urllib.parse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from myhypr.tools import _qmllint, _strip_jsonc, verify_hyprland_file  # noqa: E402

OUT = ROOT / "docs" / "examples"
HOME_CFG = Path.home() / ".config"

# Dépôts Waybar choisis à la main : (dépôt, chemins de fichiers ou de dossiers)
WAYBAR_REMOTE = {
    "Alexays/Waybar": ["resources/config.jsonc", "resources/style.css"],
    "HyDE-Project/HyDE": ["Configs/.local/share/waybar/modules/"],
    "JaKooLit/Hyprland-Dots": [
        "config/waybar/Modules", "config/waybar/ModulesCustom", "config/waybar/ModulesGroups",
        "config/waybar/ModulesWorkspaces",
        "config/waybar/configs/[TOP] Default", "config/waybar/configs/[TOP] Minimal - Long",
        "config/waybar/configs/[BOT] Simple", "config/waybar/configs/[LEFT] WestWing",
        "config/waybar/style/[Catppuccin] Mocha.css", "config/waybar/style/[Retro] Simple Style.css",
        "config/waybar/style/[Extra] Rose Pine.css", "config/waybar/style/[Extra] Modern-Combined.css",
        "config/waybar/style/[Extra] Mauve.css", "config/waybar/style/[Extra] EverForest.css",
        "config/waybar/style/[Colored] Translucent.css", "config/waybar/style/[Extra] Arrow.css",
    ],
}
MAX_LUA_FILES_PER_REPO = 60

sources: list[str] = []
kept = {"lua": 0, "waybar": 0, "css": 0, "qml": 0}
seen_hashes: set[str] = set()


def gh(*args: str) -> bytes | None:
    r = subprocess.run(["gh", "api", *args], capture_output=True)
    return r.stdout if r.returncode == 0 else None


def gh_raw(repo: str, path: str) -> bytes | None:
    return gh("-H", "Accept: application/vnd.github.raw", f"repos/{repo}/contents/{urllib.parse.quote(path)}")


def gh_list(repo: str, path: str) -> list[dict]:
    data = gh(f"repos/{repo}/contents/{urllib.parse.quote(path)}")
    try:
        items = json.loads(data) if data else []
    except json.JSONDecodeError:
        return []
    return items if isinstance(items, list) else []


def is_new(content: bytes) -> bool:
    h = hashlib.sha1(content.strip()).hexdigest()
    if h in seen_hashes:
        return False
    seen_hashes.add(h)
    return True


def lua_ok(path: Path) -> bool:
    text = path.read_text(errors="ignore")
    return "hl." in text and len(text.splitlines()) >= 8 and verify_hyprland_file(path) == "config ok"


def json_ok(text: str) -> bool:
    """JSON(C) valide et non vide (certains dépôts ont des fichiers `{}` de remplacement)."""
    try:
        return bool(json.loads(_strip_jsonc(text)))
    except json.JSONDecodeError:
        return False


def save(rel: str, content: bytes, kind: str):
    if not is_new(content):
        return
    dst = OUT / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(content)
    kept[kind] += 1


# ------------------------------------------------------------------------------- locale (ml4w)

def local_ml4w():
    hypr = (HOME_CFG / "hypr").resolve()
    n0 = kept["lua"]
    for f in sorted(hypr.glob("conf/*/*.lua")) + [hypr / n for n in ("input.lua", "gestures.lua", "functions.lua")]:
        if f.exists() and lua_ok(f):
            save(f"ml4w/hypr/{f.relative_to(hypr)}", f.read_bytes(), "lua")
    waybar = (HOME_CFG / "waybar").resolve()
    w0, c0 = kept["waybar"], kept["css"]
    if (waybar / "modules.json").exists() and json_ok((waybar / "modules.json").read_text()):
        save("ml4w/waybar/modules.json", (waybar / "modules.json").read_bytes(), "waybar")
    for theme in sorted((waybar / "themes").glob("*")):
        cfg = theme / "config"
        if cfg.exists() and json_ok(cfg.read_text()):
            save(f"ml4w/waybar/themes/{theme.name}/config.jsonc", cfg.read_bytes(), "waybar")
        for css in sorted(theme.rglob("style.css")):
            save(f"ml4w/waybar/themes/{css.relative_to(waybar / 'themes')}", css.read_bytes(), "css")
    sources.append(f"- **ml4w (local)** `{hypr}` · {kept['lua'] - n0} fichiers Lua · "
                   f"{kept['waybar'] - w0} configs Waybar · {kept['css'] - c0} CSS")


# ------------------------------------------------------------------------------- Hyprland Lua (GitHub)

def find_lua_repos() -> dict[str, str]:
    data = gh("-X", "GET", "search/code", "-f", "q=hl.config filename:hyprland.lua", "-f", "per_page=100")
    items = json.loads(data).get("items", []) if data else []
    repos: dict[str, str] = {}
    for it in items:
        repo, path = it["repository"]["full_name"], it["path"]
        if "omarchy" in repo.lower() or not path.endswith("hyprland.lua"):
            continue  # thèmes Omarchy = seulement des couleurs
        repos.setdefault(repo, path)
    return repos


def fetch_lua_dir(repo: str, directory: str, dst: Path, depth: int = 0, budget: list | None = None):
    budget = budget if budget is not None else [MAX_LUA_FILES_PER_REPO]
    for item in gh_list(repo, directory):
        if budget[0] <= 0:
            return
        if item["type"] == "file" and item["name"].endswith(".lua"):
            content = gh_raw(repo, item["path"])
            if content:
                rel = item["path"][len(directory):].lstrip("/") if directory else item["path"]
                (dst / rel).parent.mkdir(parents=True, exist_ok=True)
                (dst / rel).write_bytes(content)
                budget[0] -= 1
        elif item["type"] == "dir" and depth < 2:
            fetch_lua_dir(repo, item["path"], dst, depth + 1, budget)


def remote_lua():
    repos = find_lua_repos()
    print(f"{len(repos)} dépôts Lua candidats")
    for repo, entry in sorted(repos.items()):
        directory = entry.rsplit("/", 1)[0] if "/" in entry else ""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fetch_lua_dir(repo, directory, tmp)
            files = sorted(tmp.rglob("*.lua"))
            if not files:
                continue
            main = tmp / Path(entry).name
            whole_ok = main.exists() and verify_hyprland_file(main) == "config ok"
            before = kept["lua"]
            for f in files:
                # Config complète valide → tous ses fichiers ; sinon seulement ceux valides seuls
                if (whole_ok and "hl." in f.read_text(errors="ignore")) or lua_ok(f):
                    save(f"github/{repo.replace('/', '__')}/hypr/{f.relative_to(tmp)}", f.read_bytes(), "lua")
            n = kept["lua"] - before
            status = "config complète valide" if whole_ok else "fichiers valides seuls"
            print(f"  {'✓' if n else '✗'} {repo:<40} {n:>3} fichiers ({status})")
            if n:
                sources.append(f"- [{repo}](https://github.com/{repo}) `{entry}` · {n} fichiers Lua ({status})")


# ------------------------------------------------------------------------------- Quickshell

# (dépôt, regex des chemins à prendre)
QS_REMOTE = {
    "quickshell-mirror/quickshell-examples": r".*\.qml$",           # exemples officiels, petits et ciblés
    "caelestia-dots/shell": r"^modules/bar/.*\.qml$",              # barre d'une config populaire
}


def qml_files_ok(root: Path) -> list[Path]:
    """Fichiers QML sans erreur fiable (syntaxe, faute de propriété) selon qmllint."""
    bad = {line.split(":", 1)[0] for line in _qmllint(root)}
    return [f for f in sorted(root.rglob("*.qml")) if f.relative_to(root).as_posix() not in bad]


def quickshell_examples():
    local = (HOME_CFG / "quickshell").resolve()
    if local.exists():
        before = kept["qml"]
        for f in qml_files_ok(local):
            save(f"ml4w/quickshell/{f.relative_to(local)}", f.read_bytes(), "qml")
        sources.append(f"- **ml4w (local)** `{local}` · {kept['qml'] - before} fichiers QML")
        print(f"  ✓ ml4w quickshell (local)                   {kept['qml'] - before} fichiers QML")
    for repo, pattern in QS_REMOTE.items():
        tree = gh(f"repos/{repo}/git/trees/HEAD?recursive=1")
        paths = [t["path"] for t in json.loads(tree).get("tree", []) if t["type"] == "blob"
                 and re.match(pattern, t["path"])] if tree else []
        before = kept["qml"]
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for path in paths:
                content = gh_raw(repo, path)
                if content:
                    (tmp / path).parent.mkdir(parents=True, exist_ok=True)
                    (tmp / path).write_bytes(content)
            for f in qml_files_ok(tmp):
                save(f"github/{repo.replace('/', '__')}/quickshell/{f.relative_to(tmp)}", f.read_bytes(), "qml")
        n = kept["qml"] - before
        print(f"  {'✓' if n else '✗'} {repo:<40} {n} fichiers QML (sur {len(paths)})")
        if n:
            sources.append(f"- [{repo}](https://github.com/{repo}) · {n} fichiers QML Quickshell")


# ------------------------------------------------------------------------------- Waybar (GitHub)

def remote_waybar():
    for repo, paths in WAYBAR_REMOTE.items():
        before = (kept["waybar"], kept["css"])
        targets = []
        for p in paths:
            if p.endswith("/"):
                targets += [i["path"] for i in gh_list(repo, p.rstrip("/")) if i["type"] == "file"]
            else:
                targets.append(p)
        for path in targets:
            content = gh_raw(repo, path)
            if not content:
                print(f"  ✗ {repo}/{path} introuvable")
                continue
            name = path.split("/")[-1]
            safe = name.replace(" ", "_").replace("[", "").replace("]", "")
            if name.endswith(".css"):
                save(f"github/{repo.replace('/', '__')}/waybar/{safe}", content, "css")
            elif json_ok(content.decode(errors="ignore")):
                safe = safe if safe.endswith((".json", ".jsonc")) else safe + ".jsonc"
                save(f"github/{repo.replace('/', '__')}/waybar/{safe}", content, "waybar")
        w, c = kept["waybar"] - before[0], kept["css"] - before[1]
        print(f"  ✓ {repo:<40} {w} configs Waybar, {c} CSS")
        sources.append(f"- [{repo}](https://github.com/{repo}) · {w} configs/modules Waybar · {c} CSS")


def main():
    for sub in ("ml4w", "github"):
        shutil.rmtree(OUT / sub, ignore_errors=True)
    OUT.mkdir(parents=True, exist_ok=True)
    print("== ml4w local")
    local_ml4w()
    print("== Hyprland Lua sur GitHub")
    remote_lua()
    print("== Waybar sur GitHub")
    remote_waybar()
    print("== Quickshell")
    quickshell_examples()
    (OUT / "SOURCES.md").write_text(
        "# Sources des exemples\n\nGénéré par `scripts/fetch_examples.py`. Les fichiers eux-mêmes ne sont pas "
        "versionnés (configs tierces) : relance le script pour les récupérer.\n\n"
        "Filtre : Lua validé par `Hyprland --verify-config` de la version installée, JSON Waybar parsé, "
        "QML sans erreur de syntaxe ni faute de propriété (qmllint).\n\n"
        + "\n".join(sources) + "\n")
    print(f"\nTotal : {kept['lua']} Lua · {kept['waybar']} configs Waybar · {kept['css']} CSS · "
          f"{kept['qml']} QML → {OUT}")


if __name__ == "__main__":
    main()
