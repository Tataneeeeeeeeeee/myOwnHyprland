"""Étape 3 — outils de l'agent. Tout est confiné au dossier de travail de la session."""

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import rag
from .config import TOOL_OUTPUT_LIMIT

ANSI = re.compile(r"\x1b\[[0-9;]*m")


class Workspace:
    """Dossier sandbox d'une session : hypr/…, waybar/…, etc. (miroir de ~/.config)."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, rel: str) -> Path:
        p = (self.root / rel.lstrip("/")).resolve()
        if not p.is_relative_to(self.root):
            raise ValueError(f"Chemin hors du dossier de travail : {rel}")
        return p

    def files(self) -> list[str]:
        return sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file())

    # ------------------------------------------------------------------ outils
    # Les docstrings servent de schéma pour le tool calling d'Ollama.

    def write_file(self, path: str, content: str) -> str:
        """Écrit un fichier complet dans le dossier de travail.

        Args:
            path: Chemin relatif, ex. "hypr/hyprland.lua", "waybar/config.jsonc", "waybar/style.css"
            content: Contenu complet du fichier

        Returns:
            Confirmation
        """
        p = self.path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"OK : {path} écrit ({len(content)} caractères)"

    def read_file(self, path: str) -> str:
        """Lit un fichier du dossier de travail.

        Args:
            path: Chemin relatif du fichier

        Returns:
            Contenu du fichier
        """
        p = self.path(path)
        return p.read_text() if p.exists() else f"ERREUR : {path} n'existe pas"

    def list_files(self) -> str:
        """Liste les fichiers présents dans le dossier de travail.

        Returns:
            Un chemin par ligne
        """
        return "\n".join(self.files()) or "(dossier vide)"

    def search_docs(self, query: str) -> str:
        """Cherche dans la documentation officielle Hyprland (Lua), Waybar et l'API hl.*.

        Args:
            query: Ce que tu cherches, ex. "window rule float by class", "waybar clock format"

        Returns:
            Les passages de doc les plus pertinents
        """
        return rag.format_hits(rag.search(query, k=4))

    def verify_hyprland(self, path: str = "hypr/hyprland.lua") -> str:
        """Valide une config Hyprland Lua avec `Hyprland --verify-config` (sans toucher la session).

        Args:
            path: Fichier d'entrée Lua à valider, par défaut "hypr/hyprland.lua"

        Returns:
            "config ok" ou la liste des erreurs
        """
        p = self.path(path)
        if not p.exists():
            return f"ERREUR : {path} n'existe pas"
        return verify_hyprland_file(p)

    def verify_waybar(self, config: str = "waybar/config.jsonc", style: str = "waybar/style.css") -> str:
        """Valide une config Waybar : syntaxe JSONC puis lancement test de 3 s pour capter les erreurs.

        Args:
            config: Chemin du fichier de config Waybar
            style: Chemin du fichier CSS Waybar

        Returns:
            "waybar ok" ou la liste des erreurs
        """
        cfg = self.path(config)
        if not cfg.exists():
            return f"ERREUR : {config} n'existe pas"
        css = self.path(style)
        return verify_waybar_files(cfg, css if css.exists() else None)

    def tools(self) -> dict:
        return {f.__name__: f for f in (self.write_file, self.read_file, self.list_files,
                                         self.search_docs, self.verify_hyprland, self.verify_waybar)}

    def validate_all(self) -> list[str]:
        """Validation automatique de tout ce qui a été produit. Renvoie les erreurs."""
        errors = []
        hypr_entry = self.root / "hypr" / "hyprland.lua"
        luas = [hypr_entry] if hypr_entry.exists() else sorted(self.root.glob("hypr/**/*.lua"))
        for lua in luas:
            out = verify_hyprland_file(lua)
            if out != "config ok":
                errors.append(f"{lua.relative_to(self.root)} :\n{out}")
        for cfg in sorted(self.root.glob("waybar/config*.json*")):
            css = cfg.parent / "style.css"
            out = verify_waybar_files(cfg, css if css.exists() else None)
            if out != "waybar ok":
                errors.append(f"{cfg.relative_to(self.root)} :\n{out}")
        return errors


# ---------------------------------------------------------------- validateurs

def verify_hyprland_file(path: Path) -> str:
    try:
        r = subprocess.run(["Hyprland", "--verify-config", "-c", str(path)],
                           capture_output=True, text=True, timeout=30, cwd=path.parent)
    except subprocess.TimeoutExpired:
        return "ERREUR : la vérification a dépassé 30 s"
    out = ANSI.sub("", r.stdout + r.stderr)
    result = out.split("Config parsing result:", 1)[-1].strip()
    if result == "config ok":
        return "config ok"
    result = result.replace(str(path.parent) + "/", "")
    return result[:TOOL_OUTPUT_LIMIT] or out[-TOOL_OUTPUT_LIMIT:]


def _strip_jsonc(text: str) -> str:
    out, i, in_str = [], 0, False
    while i < len(text):
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\":
                out.append(text[i + 1:i + 2])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif text.startswith("//", i):
            i = text.find("\n", i)
            i = len(text) if i < 0 else i
            continue
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = len(text) if j < 0 else j + 2
            continue
        else:
            out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))  # virgules finales tolérées par waybar


# Erreurs Waybar dont le message est trompeur pour un petit modèle → indice ajouté au retour
WAYBAR_HINTS = {
    "invalid arg-id": "Indice : un placeholder {xxx} n'existe pas pour ce module (ex. clock n'a ni {time} ni {date} ; "
                      "{:%H:%M} est CORRECT, ne retire pas les accolades). Vérifie format, format-alt, tooltip-format.",
    "argument not found": "Indice : un placeholder {xxx} du format de ce module n'existe pas. Corrige le format "
                          "(cherche les placeholders valides avec search_docs) ; ne supprime PAS le module.",
    "Unknown module": "Indice : ce nom de module n'existe pas. Volume = \"pulseaudio\" ou \"wireplumber\", "
                      "workspaces = \"hyprland/workspaces\". Vérifie avec search_docs.",
    "Expected a valid selector": "Indice : sélecteur CSS invalide pour GTK (ex. #custom-foo, .modules-left, "
                                 "window#waybar, #workspaces button.active).",
    "not a valid property name":"Indice : propriété CSS non supportée par GTK (pas de display, flex, gap, transform…).",
}


def _lint_waybar(conf: dict) -> list[str]:
    """Erreurs silencieuses que waybar ne signale pas."""
    issues = []
    fmt = conf.get("clock", {}).get("format", "") if isinstance(conf.get("clock"), dict) else ""
    if "%" in fmt and "{:" not in fmt:
        issues.append(f"[lint] clock.format = {fmt!r} : sans {{:...}} les % s'affichent tels quels. Utilise \"{{:%H:%M}}\".")
    return issues


def verify_waybar_files(cfg: Path, css: Path | None) -> str:
    try:
        conf = json.loads(_strip_jsonc(cfg.read_text()))
    except json.JSONDecodeError as e:
        return f"ERREUR JSON dans {cfg.name} ligne {e.lineno} col {e.colno} : {e.msg}"
    lint = _lint_waybar(conf) if isinstance(conf, dict) else []
    if lint:
        return "\n".join(lint)

    if not os.environ.get("WAYLAND_DISPLAY") or not shutil.which("waybar"):
        return "waybar ok (JSON valide ; test live ignoré : pas de session Wayland)"

    cmd = ["waybar", "-c", str(cfg), "-l", "warning"]
    if css:
        cmd += ["-s", str(css)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(3)
    proc.terminate()
    try:
        out, _ = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    issues = [ANSI.sub("", line) for line in out.splitlines()
              if re.search(r"\[(error|warning|critical)\]", line, re.I)]
    if proc.returncode not in (0, -15, 143) and not issues:
        issues = [ANSI.sub("", out[-2000:])]
    issues += [hint for key, hint in WAYBAR_HINTS.items() if any(key in i for i in issues)]
    return "waybar ok" if not issues else "\n".join(issues)[:TOOL_OUTPUT_LIMIT]
