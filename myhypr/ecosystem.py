"""STATIC validation of hyprlock / hypridle / hyprpaper configs (still in hyprlang).

These tools have no verify option, and running them would affect the session (lock, sleep,
wallpaper). So we check without running them:
- hyprlang syntax (braces, "key = value");
- known categories and keys, from the tables of the installed wiki (schema built automatically);
- simple types (int, float, bool);
- referenced files (hyprpaper images, hyprlock backgrounds and images);
- commands: binary present, and old-style `hyprctl dispatch` (Lua Hyprland expects an hl.dsp expression).
"""

import difflib
import glob
import os
import re
import shutil
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .config import DOCS_RAW
from .migrate import Converter, strip_comment

WIKI = DOCS_RAW / "hyprland-wiki" / "content" / "hypr-ecosystem" / "user"

# Wiki section title → config category ("" = top level)
SECTIONS = {
    "hyprlock": {"General": "general", "Authentication": "auth", "Animations": "animations",
                 "Background": "background", "Image": "image", "Shape": "shape", "Input Field": "input-field",
                 "Label": "label", "Shadowable": "*widget", "Clickable": "*widget"},
    "hypridle": {"General": "general", "Listeners": "listener"},
    "hyprpaper": {"Setting wallpapers": "wallpaper", "Misc Options": ""},
}
WIDGETS = {"background", "image", "shape", "input-field", "label"}
# Keys described in the wiki text rather than in the tables
EXTRA = {
    "hyprlock": {"*widget": {"monitor": "string", "halign": "string", "valign": "string", "zindex": "int",
                             "position": "vec2"},
                 "animations": {"enabled": "bool"}},
    "hypridle": {}, "hyprpaper": {},
}
KEYWORDS = {"source", "bezier", "animation"}
COMMAND_KEYS = {"lock_cmd", "unlock_cmd", "on_lock_cmd", "on_unlock_cmd", "before_sleep_cmd", "after_sleep_cmd",
                "on-timeout", "on-resume", "condition_cmd", "onclick"}
PATH_KEYS = {("hyprpaper", "wallpaper", "path"), ("hyprlock", "background", "path"), ("hyprlock", "image", "path")}


@lru_cache(maxsize=None)
def schema(tool: str) -> dict[str, dict[str, str]]:
    """{category: {key: type}} extracted from the wiki's | `key` | description | type | default | tables."""
    page = WIKI / f"{tool}.md"
    out: dict[str, dict[str, str]] = {}
    if not page.exists():
        return out
    current = None
    for line in page.read_text(errors="ignore").splitlines():
        h = re.match(r"#{2,4} (.+)", line)
        if h:
            current = SECTIONS[tool].get(h.group(1).strip(), current if h.group(0).startswith("####") else None)
            continue
        row = re.match(r"\|\s*`([\w:.\-]+)`\s*\|[^|]*\|\s*([^|]+?)\s*\|", line)
        if row and current is not None:
            key, typ = row.group(1), row.group(2).lower()
            typ = next((t for t in ("int", "float", "bool", "color", "vec2", "string") if t in typ), "string")
            out.setdefault(current, {})[key] = typ
    for cat, keys in EXTRA.get(tool, {}).items():
        out.setdefault(cat, {}).update(keys)
    if "*widget" in out:  # properties shared by all hyprlock widgets
        for w in WIDGETS:
            out.setdefault(w, {}).update(out["*widget"])
        del out["*widget"]
    return out


def detect_tool(path: Path) -> str | None:
    name = path.name.lower()
    for tool in ("hyprlock", "hypridle", "hyprpaper"):
        if tool in name:
            return tool
    return None


@dataclass
class Entry:
    category: str    # "listener", "input-field", "" for the top level, "auth" …
    key: str
    value: str
    line: int
    block: int = 0   # id of the enclosing { } block (two "listener { }" = two blocks)


def parse(path: Path, issues: list[str], variables: dict[str, str] | None = None, depth: int = 0) -> list[Entry]:
    """Minimal hyprlang reader (nested sections, $variables, source)."""
    variables = {} if variables is None else variables
    entries, stack, blocks = [], [], []
    counter = [0]
    for n, original in enumerate(path.read_text(errors="ignore").splitlines(), 1):
        line = "" if original.strip().startswith("#") else strip_comment(original).strip()
        if not line:
            continue
        m = re.fullmatch(r"([\w.:\-]+)\s*\{", line)
        if m:
            stack.append(m.group(1))
            counter[0] += 1
            blocks.append(counter[0] + depth * 10000)
            continue
        if line == "}":
            if not stack:
                issues.append(f"{path.name}:{n}: accolade fermante en trop")
            else:
                stack.pop()
                blocks.pop()
            continue
        m = re.fullmatch(r"([^=]+?)\s*=\s*(.*)", line)
        if not m:
            issues.append(f"{path.name}:{n}: ligne illisible (attendu « clé = valeur ») : {original.strip()}")
            continue
        key, value = m.group(1).strip(), m.group(2).strip()
        if key.startswith("$"):
            variables[key[1:]] = value
            continue
        if key == "source" and depth < 5:
            target = os.path.expandvars(os.path.expanduser(value))
            target = target if os.path.isabs(target) else str(path.parent / target)
            found = sorted(glob.glob(target))
            if not found:
                issues.append(f"{path.name}:{n}: fichier sourcé introuvable : {value}")
            for f in found:
                entries += parse(Path(f), issues, variables, depth + 1)
            continue
        for name, v in variables.items():
            value = value.replace(f"${name}", v)
        entries.append(Entry(":".join(stack), key, value, n, blocks[-1] if blocks else 0))
    if stack:
        issues.append(f"{path.name}: section « {stack[-1]} » jamais refermée (accolade manquante)")
    return entries


def _type_ok(typ: str, value: str) -> bool:
    v = value.strip()
    if "$" in v or not v:
        return True
    if typ == "int":
        return re.fullmatch(r"-?\d+", v) is not None
    if typ == "float":
        return re.fullmatch(r"-?\d+(\.\d+)?", v) is not None
    if typ == "bool":
        return v.lower() in ("true", "false", "yes", "no", "on", "off", "0", "1")
    return True


def _old_dispatch(cmd: str) -> str | None:
    """"hyprctl dispatch dpms off" → Lua suggestion, or None if the command is already in the right format."""
    m = re.search(r"hyprctl\s+dispatch\s+([^'\"\s]\S*)\s*([^&|;]*)", cmd)
    if not m or m.group(1).startswith("hl."):
        return None
    name, args = m.group(1).lower(), m.group(2).strip()
    if name == "dpms":  # dpms off|on → action disable|enable
        args = {"off": "disable", "on": "enable"}.get(args, args)
        lua = f'hl.dsp.dpms({{ action = "{args or "toggle"}" }})'
    else:
        c = Converter(Path("."))
        c.used = set()
        lua = c.dispatcher(name, args)
    return f"hyprctl dispatch '{lua}'" if lua else "hyprctl dispatch '<expression hl.dsp.…>' (voir search_docs dispatchers)"


def verify(path: Path) -> str:
    """"ok", or errors (blocking) followed by warnings (non-blocking)."""
    tool = detect_tool(path)
    if tool is None:
        return f"ERREUR : impossible de savoir si {path.name} est pour hyprlock, hypridle ou hyprpaper"
    errors: list[str] = []
    warnings: list[str] = []
    entries = parse(path, errors)
    sch = schema(tool)

    for e in entries:
        where = f"{path.name}:{e.line}"
        base = e.category.split(":")[0]
        if tool == "hyprpaper" and not e.category and e.key in ("preload", "wallpaper", "unload"):
            errors.append(f"{where}: « {e.key} = … » est l'ancienne syntaxe de hyprpaper (< 0.8). Écris un bloc "
                          "wallpaper { monitor = …  path = …  fit_mode = cover } (monitor vide = tous les écrans).")
            continue
        if e.key in KEYWORDS and (tool == "hyprlock" and base == "animations" or e.key == "source"):
            continue
        if base not in sch:
            known = [c or "(racine)" for c in sch]
            hint = difflib.get_close_matches(base, list(sch), n=1)
            errors.append(f"{where}: catégorie « {base or '(racine)'} » inconnue pour {tool}"
                          + (f" → vouliez-vous dire « {hint[0]} » ?" if hint else f". Connues : {', '.join(known)}"))
            continue
        # hyprlock writes "pam:enabled" in auth { } or auth { pam { enabled } }: accept both
        sub = e.category.split(":")[1:]
        key = ":".join([*sub, e.key])
        keys = sch[base]
        if key not in keys:
            hint = difflib.get_close_matches(key, list(keys), n=1, cutoff=0.6)
            errors.append(f"{where}: option « {key} » inconnue dans {base or 'la racine'}"
                          + (f" → vouliez-vous dire « {hint[0]} » ?" if hint else
                             f". Options valides : {', '.join(sorted(keys))[:300]}"))
            continue
        if not _type_ok(keys[key], e.value):
            errors.append(f"{where}: {key} attend un {keys[key]}, reçu « {e.value} »")
        if (tool, base, e.key) in PATH_KEYS and e.value and e.value != "screenshot":
            p = os.path.expandvars(os.path.expanduser(e.value))
            if not os.path.exists(p):
                (errors if tool == "hyprpaper" else warnings).append(f"{where}: fichier introuvable : {e.value}")
        if tool == "hyprlock" and e.key == "text" and re.search(r"%[a-zA-Z%]", e.value) and "cmd[" not in e.value:
            errors.append(f"{where}: hyprlock n'interprète pas les formats de date (« {e.value} »). Pour l'heure : "
                          "text = $TIME (ou $TIME12) ; pour un format libre : "
                          "text = cmd[update:1000] date +\"%H:%M\"")
        if e.key in COMMAND_KEYS and e.value:
            fix = _old_dispatch(e.value)
            if fix:
                errors.append(f"{where}: « {e.value} » : avec Hyprland en Lua, hyprctl dispatch attend une expression "
                              f"Lua → {fix}")
            binary = re.split(r"[\s;&|]+", e.value.strip())[0]
            if binary and not binary.startswith(("$", "~", "/", ".")) and not shutil.which(binary) \
                    and binary not in ("pidof", "loginctl", "systemctl"):
                warnings.append(f"{where}: commande « {binary} » introuvable sur ce système")

    if tool == "hypridle":
        blocks: dict[int, list[Entry]] = {}
        for e in entries:
            if e.category == "listener":
                blocks.setdefault(e.block, []).append(e)
        for b in blocks.values():
            if not any(x.key == "timeout" for x in b):
                errors.append(f"{path.name}:{b[0].line}: bloc listener sans « timeout » (obligatoire, en secondes)")
    if tool == "hyprpaper" and not any(e.category == "wallpaper" and e.key == "path" for e in entries):
        warnings.append(f"{path.name}: aucun bloc wallpaper {{ path = … }} : aucun fond ne sera affiché")

    if errors:
        return "\n".join(errors + [f"(avertissement) {w}" for w in warnings])
    if warnings:
        return f"{tool} ok (avertissements non bloquants) :\n" + "\n".join(warnings)
    return f"{tool} ok"
