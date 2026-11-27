"""Keybind conflict detection: two binds on the same combo, in the same submap.

Two sources:
- a directory's Lua config (run with the fake hl API, see hl_mock.lua) → conflicts BEFORE applying;
- the running Hyprland session (`hyprctl binds -j`) → conflicts in the config actually loaded.
"""

import json
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

MOCK = Path(__file__).with_name("hl_mock.lua")

MOD_ALIASES = {"SUPER": "SUPER", "WIN": "SUPER", "LOGO": "SUPER", "MOD4": "SUPER", "META": "SUPER",
               "CTRL": "CTRL", "CONTROL": "CTRL", "ALT": "ALT", "MOD1": "ALT", "SHIFT": "SHIFT",
               "CAPS": "CAPS", "MOD2": "MOD2", "MOD3": "MOD3", "MOD5": "MOD5"}
MOD_ORDER = ["SUPER", "CTRL", "ALT", "SHIFT", "CAPS", "MOD2", "MOD3", "MOD5"]
MODMASK = {1: "SHIFT", 2: "CAPS", 4: "CTRL", 8: "ALT", 16: "MOD2", 32: "MOD3", 64: "SUPER", 128: "MOD5"}


@dataclass(frozen=True)
class Bind:
    combo: str          # normalized "SUPER + SHIFT + Q"
    submap: str
    trigger: str        # "appui" (press) | "relâchement" (release) | "souris" (mouse) | "appui long" (long press)
    action: str
    where: str          # file:line (config) or description (session)
    text: str = ""      # exact code line (config): to copy verbatim into edit_file

    @property
    def key(self) -> tuple:
        return self.submap, self.combo, self.trigger


def normalize(keys: str) -> str:
    parts = []
    for token in (t for t in re.split(r"\s*[+,]\s*", str(keys).strip()) if t):
        words = token.split()
        # "SUPER SHIFT" (hyprlang style) → two modifiers; "switch:on:Lid Switch" stays a single key
        parts += words if all(w.upper() in MOD_ALIASES for w in words) else [token]
    mods = sorted({MOD_ALIASES[p.upper()] for p in parts if p.upper() in MOD_ALIASES}, key=MOD_ORDER.index)
    rest = [p for p in parts if p.upper() not in MOD_ALIASES]
    key = rest[-1].upper() if rest else "?"
    return " + ".join([*mods, key])


def _trigger(opts: dict | None) -> str:
    opts = opts or {}
    if opts.get("mouse") or opts.get("drag") or opts.get("click"):
        return "souris"
    if opts.get("long_press"):
        return "appui long"
    return "relâchement" if opts.get("release") else "appui"


def _action(action) -> str:
    if isinstance(action, dict) and "dsp" in action:
        args = action.get("args") or []
        shown = ", ".join(a if isinstance(a, str) else json.dumps(a, ensure_ascii=False) for a in args)
        return f"{action['dsp']}({shown})"
    return "fonction Lua" if action == "<function>" else str(action)


def from_config(entry: Path) -> list[Bind]:
    """Keybinds declared by a Lua config (without loading it into Hyprland)."""
    if not entry.exists():
        return []
    try:
        r = subprocess.run(["lua", str(MOCK), str(entry)], capture_output=True, text=True, timeout=20)
        binds = json.loads(r.stdout).get("binds", [])
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return []
    out = []
    for b in binds:
        src, line = b.get("src", "?"), b.get("line", 0)
        try:
            text = (entry.parent / src).read_text(errors="ignore").splitlines()[line - 1].strip()
        except (OSError, IndexError):
            text = ""
        out.append(Bind(normalize(b.get("keys", "")), b.get("submap", ""), _trigger(b.get("opts")),
                        _action(b.get("action")), f"{src}:{line}", text))
    return out


def from_session() -> list[Bind]:
    """Keybinds of the config loaded by the running Hyprland session."""
    try:
        r = subprocess.run(["hyprctl", "binds", "-j"], capture_output=True, text=True, timeout=5)
        binds = json.loads(r.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return []
    out = []
    for b in binds:
        mods = [name for bit, name in MODMASK.items() if b.get("modmask", 0) & bit]
        key = b.get("key") or (f"code:{b['keycode']}" if b.get("keycode") else "?")
        trigger = "souris" if b.get("mouse") else "appui long" if b.get("longPress") else \
            "relâchement" if b.get("release") else "appui"
        out.append(Bind(normalize(" + ".join([*mods, key])), b.get("submap", ""), trigger,
                        f"{b.get('dispatcher')} {b.get('arg', '')}".strip(), b.get("description") or "session"))
    return out


def _groups(binds: list[Bind]) -> dict[tuple, list[Bind]]:
    groups: dict[tuple, list[Bind]] = defaultdict(list)
    for b in binds:
        groups[b.key].append(b)
    return {k: v for k, v in groups.items() if len(v) > 1}


def conflicts(binds: list[Bind]) -> dict[tuple, list[Bind]]:
    """Same combo, DIFFERENT actions: only one of them will actually fire."""
    return {k: v for k, v in _groups(binds).items() if len({b.action for b in v}) > 1}


def duplicates(binds: list[Bind]) -> dict[tuple, list[Bind]]:
    """Same combo, same action: harmless, just redundant."""
    return {k: v for k, v in _groups(binds).items() if len({b.action for b in v}) == 1}


def describe(found: dict[tuple, list[Bind]]) -> list[str]:
    lines = []
    for (submap, combo, trigger), group in sorted(found.items()):
        where = f" (sous-mode {submap})" if submap else ""
        when = "" if trigger == "appui" else f" [{trigger}]"
        uses = " ; ".join(f"{b.action} @ {b.where}" for b in group)
        lines.append(f"{combo}{when}{where} est utilisé {len(group)} fois : {uses}")
    return lines


def lookup(entry: Path, keys: str) -> str:
    """Is the combo free? If not: who uses it, where, and the exact line."""
    combo = normalize(keys)
    uses = [b for b in from_config(entry) if b.combo == combo]
    if not uses:
        return f"{combo} est LIBRE (aucun raccourci ne l'utilise)."
    lines = [f"{combo} est DÉJÀ UTILISÉ :"]
    for b in uses:
        sub = f" (sous-mode {b.submap})" if b.submap else ""
        lines.append(f"- {b.action}{sub} @ {b.where} : {b.text}")
    return "\n".join(lines)


def report(entry: Path) -> str:
    """Text for the agent: conflicts + number of keybinds analyzed."""
    binds = from_config(entry)
    found = conflicts(binds)
    dup = duplicates(binds)
    if not found:
        return f"aucun conflit ({len(binds)} raccourcis analysés" + (f", {len(dup)} doublon(s) inoffensif(s)" if dup else "") + ")"
    return "\n".join(["CONFLITS DE RACCOURCIS :", *describe(found),
                      "Une même combinaison ne doit déclencher qu'une action : change la touche de l'un, "
                      "ou remplace l'ancien raccourci si la demande le veut."])
