"""Wallpaper-based theming: Material 3 palette computed by matugen, with no side effects.

- `current_wallpaper()`: the wallpaper currently shown (awww / swww / hyprpaper / quickshell state);
- `palette()`: colours via `matugen --dry-run` (writes nothing, restarts nothing);
- `verify_matugen()`: renders the templates of a matugen config in a sandbox (`--prefix`), WITHOUT the hooks
  (post_hook/pre_hook stripped), to catch template errors before applying.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from functools import lru_cache
from pathlib import Path

# Roles useful for a config (the rest of the 50 M3 roles are available through the tool in full mode)
MAIN_ROLES = ["primary", "on_primary", "primary_container", "secondary", "tertiary", "surface",
              "surface_container", "surface_container_high", "on_surface", "on_surface_variant", "outline",
              "outline_variant", "error"]
SCHEMES = ["scheme-tonal-spot", "scheme-content", "scheme-expressive", "scheme-fidelity", "scheme-fruit-salad",
           "scheme-monochrome", "scheme-neutral", "scheme-rainbow", "scheme-vibrant"]


def current_wallpaper() -> str | None:
    """Path of the displayed wallpaper, based on whichever daemon is running."""
    forced = os.environ.get("MYHYPR_WALLPAPER")
    if forced:
        return forced
    for daemon in ("awww", "swww"):
        if shutil.which(daemon):
            try:
                out = subprocess.run([daemon, "query"], capture_output=True, text=True, timeout=5).stdout
            except (OSError, subprocess.TimeoutExpired):
                continue
            m = re.search(r"image:\s*(\S.*)$", out, re.M)
            if m and Path(m.group(1).strip()).exists():
                return m.group(1).strip()
    try:
        out = subprocess.run(["hyprctl", "hyprpaper", "listactive"], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"=\s*(/\S+)", out)
        if m and Path(m.group(1)).exists():
            return m.group(1)
    except (OSError, subprocess.TimeoutExpired):
        pass
    state = Path.home() / ".local/state/quickshell/user/generated/wallpaper/path.txt"
    if state.exists():
        p = state.read_text().strip()
        if p and Path(p).exists():
            return p
    return None


@lru_cache(maxsize=32)
def _palette(image: str, mtime: float, mode: str, scheme: str) -> dict[str, str]:
    r = subprocess.run(["matugen", "image", image, "--dry-run", "-j", "hex", "-m", mode, "-t", scheme,
                        "--prefer", "saturation"], capture_output=True, text=True, timeout=60, cwd=tempfile.gettempdir())
    if r.returncode != 0:
        raise RuntimeError(re.sub(r"\x1b\[[0-9;]*m", "", r.stderr).strip().splitlines()[-1] if r.stderr else "échec")
    colors = json.loads(r.stdout)["colors"]
    return {role: v.get(mode, v["default"])["color"] for role, v in colors.items()}


def palette(image: str, mode: str = "dark", scheme: str = "scheme-tonal-spot") -> dict[str, str]:
    image = os.path.expanduser(image)
    if not Path(image).exists():
        raise FileNotFoundError(f"image introuvable : {image}")
    if not shutil.which("matugen"):
        raise RuntimeError("matugen n'est pas installé")
    return _palette(image, Path(image).stat().st_mtime, mode, scheme)


def describe(image: str, colors: dict[str, str], full: bool = False) -> str:
    roles = colors if full else {r: colors[r] for r in MAIN_ROLES if r in colors}
    lines = [f"Palette Material 3 de {image} :"]
    lines += [f"  {role:<24} {hexa}" for role, hexa in roles.items()]
    lines.append("Usage : bordure active = primary → tertiary ; fond de barre = surface / surface_container ; "
                 "texte = on_surface ; éléments actifs = primary (texte on_primary) ; séparateurs = outline_variant. "
                 "Hyprland : \"rgba(RRGGBBff)\" ; CSS Waybar : #rrggbb ou @define-color.")
    return "\n".join(lines)


def hexes(colors: dict[str, str]) -> set[str]:
    return {c.lower().lstrip("#") for c in colors.values()}


def _css_lint(css: str) -> list[str]:
    """Common GTK CSS errors in a rendered colour file (Waybar would reject the whole stylesheet)."""
    issues = []
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for n, line in enumerate(body.splitlines(), 1):
        if line.strip().startswith("@define-color") and not line.rstrip().endswith(";"):
            issues.append(f"ligne {n} : « {line.strip()} » doit finir par « ; » (GTK CSS)")
    if body.count("{") != body.count("}"):
        issues.append("accolades { } déséquilibrées")
    return issues


def verify_matugen(config: Path, image: str, workspace_root: Path | None = None) -> str:
    """Renders the templates in a sandbox. Input paths under "~/.config/…" are read from the workspace
    if it contains a version of them (what the agent just wrote)."""
    try:
        data = tomllib.loads(config.read_text())
    except tomllib.TOMLDecodeError as e:
        return f"ERREUR TOML dans {config.name} : {e}"
    templates = data.get("templates", {})
    if not templates:
        return "ERREUR : aucune section [templates.<nom>] dans la config matugen"
    issues, sandbox = [], Path(tempfile.mkdtemp(prefix="myhypr-matugen-"))
    clean = {"config": {"version_check": False}, "templates": {}}
    for name, t in templates.items():
        if "input_path" not in t or "output_path" not in t:
            issues.append(f"[templates.{name}] : input_path et output_path sont obligatoires")
            continue
        src = Path(os.path.expanduser(t["input_path"]))
        if workspace_root is not None:
            cfg_home = Path(os.path.expanduser("~/.config"))
            if src.is_relative_to(cfg_home) and (workspace_root / src.relative_to(cfg_home)).exists():
                src = workspace_root / src.relative_to(cfg_home)
        if not src.exists():
            issues.append(f"[templates.{name}] : template introuvable : {t['input_path']}")
            continue
        clean["templates"][name] = {"input_path": str(src), "output_path": t["output_path"]}  # hooks stripped
    if issues:
        return "\n".join(issues)
    cfg = sandbox / "config.toml"
    cfg.write_text("\n".join(
        ["[config]", "version_check = false"]
        + [f"[templates.{n}]\ninput_path = {json.dumps(t['input_path'])}\noutput_path = {json.dumps(t['output_path'])}"
           for n, t in clean["templates"].items()]))
    out = sandbox / "out"
    r = subprocess.run(["matugen", "image", os.path.expanduser(image), "-c", str(cfg), "--prefix", str(out),
                        "--prefer", "saturation", "-m", "dark"], capture_output=True, text=True, timeout=120,
                       cwd=tempfile.gettempdir())
    log = re.sub(r"\x1b\[[0-9;]*m", "", r.stdout + r.stderr)
    if r.returncode != 0:
        detail = [ln.strip() for ln in log.splitlines() if ln.strip() and "folder doesnt exist" not in ln]
        issues.append("matugen n'a pas pu rendre les templates : " + " | ".join(detail[-6:])
                      + "\nIndice : variables valides = {{colors.<rôle>.<default|dark|light>.<hex|hex_stripped|rgb|rgba>}},"
                        " rôles = primary, on_primary, secondary, tertiary, surface, surface_container, on_surface, "
                        "outline, error…")
    else:
        for name, t in clean["templates"].items():
            rendered = out / Path(os.path.expanduser(t["output_path"])).relative_to("/")
            if not rendered.exists():
                issues.append(f"[templates.{name}] : rien n'a été produit pour {t['output_path']}")
            elif "{{" in rendered.read_text(errors="ignore"):
                issues.append(f"[templates.{name}] : des {{{{…}}}} n'ont pas été remplacés dans le résultat")
            elif rendered.suffix == ".css":
                issues += [f"[templates.{name}] : {e}" for e in _css_lint(rendered.read_text(errors="ignore"))]
            elif rendered.suffix == ".lua":
                from .tools import verify_hyprland_file
                res = verify_hyprland_file(rendered)
                if res != "config ok":
                    issues.append(f"[templates.{name}] : le Lua généré est invalide : {res.splitlines()[0]}")
    shutil.rmtree(sandbox, ignore_errors=True)
    return "\n".join(issues) if issues else f"matugen ok ({len(clean['templates'])} template(s) rendus sans erreur)"


def matugen_outputs(config: Path | None = None) -> list[str]:
    """Files GENERATED by the user's matugen config (not to be edited by hand)."""
    config = config or Path.home() / ".config/matugen/config.toml"
    try:
        data = tomllib.loads(config.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return []
    return [t["output_path"] for t in data.get("templates", {}).values() if "output_path" in t]
