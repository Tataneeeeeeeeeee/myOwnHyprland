"""Étape 3 — outils de l'agent. Tout est confiné au dossier de travail de la session."""

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import rag
from .config import DOCS_RAW, TOOL_OUTPUT_LIMIT

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
        if p.exists():
            old_lines, new_lines = len(p.read_text().splitlines()), len(content.splitlines())
            if old_lines >= 30 and new_lines < 0.6 * old_lines:
                return (f"ERREUR : write_file réduirait {path} de {old_lines} à {new_lines} lignes, le reste de la "
                        f"config serait perdu. Refusé. Pour modifier, utilise edit_file sur les lignes concernées.")
        p.parent.mkdir(parents=True, exist_ok=True)
        previous = p.read_text() if p.exists() else None
        p.write_text(content)
        return self._guard(path, previous) or f"OK : {path} écrit ({len(content)} caractères)"

    def edit_file(self, path: str, old: str, new: str) -> str:
        """Modifie un fichier existant en remplaçant un passage exact. À préférer à write_file pour toute retouche.

        Args:
            path: Chemin relatif du fichier à modifier
            old: Texte exact à remplacer, copié du fichier (doit y apparaître une seule fois ; ajoute du contexte sinon)
            new: Texte de remplacement

        Returns:
            Confirmation ou erreur
        """
        p = self.path(path)
        if not p.exists():
            return f"ERREUR : {path} n'existe pas (utilise write_file pour le créer)"
        text = p.read_text()
        count = text.count(old)
        if count == 0:
            return f"ERREUR : texte introuvable dans {path}. Relis le fichier avec read_file et copie le passage exact."
        if count > 1:
            return f"ERREUR : texte trouvé {count} fois dans {path}. Ajoute des lignes de contexte pour le rendre unique."
        p.write_text(text.replace(old, new, 1))
        if (refused := self._guard(path, text)):
            return refused
        return f"OK : {path} modifié ({old.count(chr(10)) + 1} ligne(s) → {new.count(chr(10)) + 1})"

    def _guard(self, path: str, previous: str | None) -> str | None:
        """Annule une modification qui casse une config Hyprland qui était valide avant."""
        entry = self.root / "hypr" / "hyprland.lua"
        if previous is None or not path.startswith("hypr/") or not path.endswith(".lua") or not entry.exists():
            return None
        p = self.path(path)
        new = p.read_text()
        after = verify_hyprland_file(entry)
        if after == "config ok":
            return None
        p.write_text(previous)
        if verify_hyprland_file(entry) != "config ok":  # c'était déjà cassé avant : on garde la modification
            p.write_text(new)
            return None
        return (f"ERREUR : cette modification casse la config, elle a été ANNULÉE ({path} est inchangé).\n{after}\n"
                f"Relis le passage avec read_file et refais une modification plus précise.")

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
        """Cherche dans la documentation officielle Hyprland (Lua), Waybar, l'API hl.* et des configs réelles.

        Args:
            query: Ce que tu cherches, ex. "window rule float by class", "waybar clock format"

        Returns:
            Les passages de doc les plus pertinents
        """
        return rag.search_mixed(query, docs=3, examples=1)

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

    def verify_quickshell(self, path: str = "quickshell/bar") -> str:
        """Valide une config Quickshell (QML) : qmllint puis lancement test de 4 s avec `qs -p`.

        Args:
            path: Dossier de la config (contenant shell.qml), ex. "quickshell/bar"

        Returns:
            "quickshell ok" ou la liste des erreurs
        """
        p = self.path(path)
        if p.is_file():
            p = p.parent
        if not (p / "shell.qml").exists():
            return f"ERREUR : {path}/shell.qml n'existe pas (une config Quickshell = un dossier avec shell.qml)"
        return verify_quickshell_dir(p)

    def tools(self) -> dict:
        return {f.__name__: f for f in (self.write_file, self.edit_file, self.read_file, self.list_files,
                                         self.search_docs, self.verify_hyprland, self.verify_waybar,
                                         self.verify_quickshell)}

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
        for shell in sorted(self.root.glob("quickshell/**/shell.qml")):
            out = verify_quickshell_dir(shell.parent)
            if not out.startswith("quickshell ok"):
                errors.append(f"{shell.parent.relative_to(self.root)} :\n{out}")
        return errors


# ---------------------------------------------------------------- validateurs

STUBS = Path("/usr/share/hypr/stubs/hl.meta.lua")


def _stub_names() -> tuple[list[str], list[str]]:
    """(fonctions de hl.*, clés de config valides) lues dans les stubs installés."""
    if not STUBS.exists():
        return [], []
    text = STUBS.read_text(errors="ignore")
    api = re.search(r"---@class HL\.API\n(.*?)\nlocal", text, re.S)
    funcs = re.findall(r"---@field (\w+) fun", api.group(1)) if api else []
    keys_block = re.search(r'---@alias HL\.ConfigKey\n(.*?)\n\n', text, re.S)
    keys = re.findall(r'"([\w.:-]+)"', keys_block.group(1)) if keys_block else []
    return funcs, keys


def _hypr_hints(out: str) -> list[str]:
    hints = []
    if re.search(r"module '(hl|hyprland)' not found|error loading module 'hyprland'|C stack overflow", out):
        hints.append("Indice : `hl` est une variable GLOBALE fournie par Hyprland. Supprime tout require(\"hl\") "
                     "ou require(\"hyprland\") : le fichier ne doit jamais se require lui-même.")
    funcs, keys = _stub_names()
    for name in dict.fromkeys(re.findall(r"attempt to call a nil value \(field '(\w+)'\)", out)):
        hints.append(f"Indice : hl.{name} n'existe pas. Fonctions hl.* disponibles : "
                     + ", ".join(f for f in funcs if not f.startswith("get_")) + ". Actions de bind : hl.dsp.*")
    for name in dict.fromkeys(re.findall(r"attempt to call a nil value \(global '(\w+)'\)", out)):
        hints.append(f"Indice : `{name}` n'est défini nulle part : texte probablement coupé par une modification "
                     f"ou fonction inventée. Relis la ligne indiquée avec read_file.")
    for key in dict.fromkeys(re.findall(r"unknown config key '([\w.:-]+)'", out)):
        section = key.rsplit(".", 1)[0]
        valid = [k.rsplit(".", 1)[-1] for k in keys if k.rsplit(".", 1)[0] == section]
        if valid:
            hints.append(f"Indice : clés valides dans {section} : " + ", ".join(valid[:60]))
        else:
            sections = sorted({k.split(".")[0] for k in keys})
            hints.append(f"Indice : section '{section}' inconnue. Sections valides : " + ", ".join(sections))
    return hints


MOCK = Path(__file__).with_name("hl_mock.lua")


def _lint_hypr(entry: Path) -> list[str]:
    """Erreurs silencieuses : `gaps_in = 10` ou `general = {...}` en variable globale sont du Lua valide
    mais n'ont AUCUN effet (Hyprland ne lit que les appels hl.*). On exécute la config avec une fausse
    API hl (hl_mock.lua) pour lister les globales réellement créées."""
    if not shutil.which("lua"):
        return []
    try:
        r = subprocess.run(["lua", str(MOCK), str(entry)], capture_output=True, text=True, timeout=20)
        created = json.loads(r.stdout).get("globals", [])
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return []
    funcs, keys = _stub_names()
    leaf = {}
    for k in keys:
        leaf.setdefault(k.rsplit(".", 1)[-1], k)
    sections = {k.split(".")[0] for k in keys} | {"config", "monitor", "binds", "bind", "env", "exec"}
    issues = []
    for name in sorted(created):
        target = next((k for k in keys if k == name.replace("_", ".")), None) or leaf.get(name)
        if not (target or name in sections or name in funcs):
            continue
        if target:
            sec, _, opt = target.rpartition(".")
            parts = sec.split(".")
            fix = "hl.config({ " + "".join(f"{p} = {{ " for p in parts) + f"{opt} = ..." + " }" * len(parts) + " })"
        elif name in funcs:
            fix = f"hl.{name}({{ ... }})"
        else:
            fix = "hl.config({ ... })" if name == "config" else f"hl.config({{ {name} = {{ ... }} }})"
        issues.append(f"`{name} = ...` crée une variable globale IGNORÉE par Hyprland (aucun effet). "
                      f"Écris plutôt : {fix}")
    return issues


def verify_hyprland_file(path: Path) -> str:
    path = path.resolve()
    try:
        r = subprocess.run(["Hyprland", "--verify-config", "-c", str(path)],
                           capture_output=True, text=True, timeout=30, cwd=path.parent)
    except subprocess.TimeoutExpired:
        return "ERREUR : la vérification a dépassé 30 s"
    out = ANSI.sub("", r.stdout + r.stderr)
    result = out.split("Config parsing result:", 1)[-1].strip()
    if result == "config ok":
        lint = _lint_hypr(path)
        return "\n".join(["[lint] la config se charge mais certaines lignes n'ont aucun effet :", *lint]) \
            if lint else "config ok"
    result = (result.replace(str(path.parent) + "/", "") or out[-3000:])
    result = "\n".join(ln for ln in result.splitlines() if not re.match(r"\s*no (field|file) ", ln))
    return "\n".join([result[:3000], *_hypr_hints(out)])[:TOOL_OUTPUT_LIMIT]


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


def _format_doc(module: str) -> str:
    """Section FORMAT REPLACEMENTS de la page wiki du module (placeholders valides)."""
    base, _, sub = module.partition("/")
    wiki = DOCS_RAW / "waybar-wiki"
    page = next((p for p in wiki.glob("Module:-*.md") if p.stem.lower() == f"module:-{base.lower()}"), None)
    if page is None:
        return ""
    text = page.read_text(errors="ignore")
    sections = []
    for m in re.finditer(r"^#{1,3} FORMAT REPLACEMENTS\n(.*?)(?=\n#{1,3} |\Z)", text, re.S | re.M):
        before = text[max(0, m.start() - 4000):m.start()].lower()
        if not sub or sub.lower() in before:
            sections.append(m.group(1).strip())
    body = re.sub(r"\*\*", "", "\n".join(sections[-1:] or sections))
    return body[:700]


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

    if os.environ.get("MYHYPR_NO_LIVE") or not os.environ.get("WAYLAND_DISPLAY") or not shutil.which("waybar"):
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
    for module in dict.fromkeys(re.findall(r"\]\s*([\w/-]+): (?:argument not found|format error)", out)):
        doc = _format_doc(module)
        if doc:
            issues.append(f"Placeholders valides pour {module} (doc officielle) :\n{doc}")
    if re.search(r"Unknown module: custom-", out):
        issues.append("Indice : les modules personnalisés s'écrivent \"custom/<nom>\" (avec un slash), "
                      "dans modules-* ET comme clé de configuration.")
    return "waybar ok" if not issues else "\n".join(issues)[:TOOL_OUTPUT_LIMIT]


# ---------------------------------------------------------------- Quickshell

QMLLINT = next((c for c in ("/usr/lib/qt6/bin/qmllint", shutil.which("qmllint6") or "") if c and Path(c).exists()), None)
QS_NOISE = re.compile(r"built against Qt|Shell ID|Saving logs|Launching config|Configuration Loaded|^\s*$")
QS_IMPORTS = ("Indice : type inconnu = import manquant. PanelWindow, FloatingWindow, PopupWindow, SystemClock, Variants, "
              "ShellRoot, Scope → import Quickshell ; Hyprland, HyprlandWorkspace → import Quickshell.Hyprland ; "
              "Process, FileView, IpcHandler → import Quickshell.Io ; Pipewire → import Quickshell.Services.Pipewire ; "
              "UPower → import Quickshell.Services.UPower ; Mpris → import Quickshell.Services.Mpris ; "
              "SystemTray → import Quickshell.Services.SystemTray ; Text, Rectangle, Item, MouseArea, Repeater, Row → "
              "import QtQuick ; RowLayout, ColumnLayout → import QtQuick.Layouts.")


# Erreurs QML fréquentes d'un petit modèle → indice précis (règles générales de QML, pas spécifiques à un cas)
QML_HINTS = [
    (r'non-existent property "(centerIn|fill|verticalCenter|horizontalCenter|leftMargin|rightMargin|topMargin|'
     r'bottomMargin|margins)"',
     "Indice : les ancrages s'écrivent avec le préfixe anchors. : anchors.centerIn: parent, anchors.fill: parent, "
     "anchors.verticalCenter: parent.verticalCenter, anchors.leftMargin: 8. Dans un RowLayout/ColumnLayout, "
     "pas d'anchors sur les enfants : utilise Layout.alignment / Layout.fillWidth."),
    (r'non-existent property "(fontSize|pixelSize|pointSize|bold|family|fontFamily|fontWeight)"',
     "Indice : la police se règle via le groupe font : font.pixelSize: 14, font.bold: true, font.family: \"...\"."),
    (r'non-existent property "(textColor|fontColor|foreground|backgroundColor|background)"',
     "Indice : Text → color ; fond → un Rectangle avec color (Rectangle n'a pas de backgroundColor)."),
    (r"Optional chains are not permitted on the left-hand-side",
     "Indice : pas de ?. à gauche d'une affectation. Écris : if (obj) obj.prop = valeur"),
    (r"cannot be created directly",
     "Indice : ce type ne s'instancie pas, on l'obtient par une propriété (ex. Pipewire.defaultAudioSink.audio, "
     "UPower.displayDevice). Pour suivre un nœud Pipewire : PwObjectTracker { objects: [Pipewire.defaultAudioSink] }"),
    (r"ReferenceError: (\w+) is not defined",
     "Indice : identifiant inconnu → donne un id à l'objet (id: monId) ou déclare property var monNom ; "
     "les propriétés d'un autre objet s'écrivent idObjet.propriete."),
    (r"Cannot read property '\w+' of (undefined|null)",
     "Indice : objet pas encore prêt au démarrage → protège avec ?. en lecture (a?.b) ou une valeur par défaut (a ?? 0)."),
]


def _qmllint(cfg_dir: Path) -> list[str]:
    """Seulement ce que qmllint signale de façon fiable : syntaxe, et propriétés inconnues AVEC suggestion
    (faute de frappe). Le reste (imports qs.*, unqualified…) est trop bruité sur des configs réelles."""
    if not QMLLINT:
        return []
    issues = []
    for f in sorted(cfg_dir.rglob("*.qml")):
        r = subprocess.run([QMLLINT, str(f)], capture_output=True, text=True, timeout=60)
        lines = (r.stdout + r.stderr).splitlines()
        for i, line in enumerate(lines):
            m = re.match(r"Warning: .*?:(\d+):(\d+): (.*) \[(syntax|missing-property)\]$", line)
            if not m:
                continue
            nxt = " ".join(lines[i + 1:i + 4])
            hint = re.search(r'Did you mean "([^"]+)"', nxt)
            if m.group(4) == "syntax":
                issues.append(f"{f.relative_to(cfg_dir)}:{m.group(1)}: {m.group(3)}")
            elif hint:
                issues.append(f"{f.relative_to(cfg_dir)}:{m.group(1)}: {m.group(3)} → vouliez-vous dire \"{hint.group(1)}\" ?")
    return issues


def verify_quickshell_dir(cfg_dir: Path) -> str:
    cfg_dir = cfg_dir.resolve()
    issues, warnings = _qmllint(cfg_dir), []
    if any("Expected token" in i or "Syntax" in i or "syntax" in i or "not permitted" in i for i in issues):
        text = "\n".join(issues)
        issues += [hint for pattern, hint in QML_HINTS if re.search(pattern, text)]
        return "\n".join(issues)[:TOOL_OUTPUT_LIMIT]

    if not os.environ.get("MYHYPR_NO_LIVE") and os.environ.get("WAYLAND_DISPLAY") and shutil.which("qs"):
        proc = subprocess.Popen(["qs", "-p", str(cfg_dir), "--no-color"], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        time.sleep(4)
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
        loaded = "Configuration Loaded" in out
        for line in out.splitlines():
            if QS_NOISE.search(line):
                continue
            if loaded and re.search(r"Unable to assign \[undefined\]", line):
                # Bénin : une liaison vaut undefined au démarrage (service pas encore prêt), le widget marche
                warnings.append(line.strip().replace(str(cfg_dir) + "/", ""))
                continue
            if re.search(r"ERROR|ReferenceError|TypeError|Error:|Cannot assign|Unable to assign|is not a type|"
                         r"is not installed|not found|Invalid", line):
                issues.append(line.strip().replace(str(cfg_dir) + "/", "").replace("file://", ""))
        if not loaded and not issues:
            issues.append("La config ne s'est pas chargée :\n" + out[-1500:])
        text = "\n".join(issues)
        if "is not a type" in text or "is not installed" in text:
            issues.append(QS_IMPORTS)
        specific = [hint for pattern, hint in QML_HINTS if re.search(pattern, text)]
        issues += specific
        if "non-existent property" in text and not specific:
            issues.append("Indice : propriété inexistante sur ce type. Vérifie le nom exact avec search_docs "
                          "(ex. \"PanelWindow properties\"). Taille d'un PanelWindow : implicitWidth / implicitHeight.")
    if issues:
        return "\n".join(dict.fromkeys(issues))[:TOOL_OUTPUT_LIMIT]
    if warnings:
        return ("quickshell ok (avertissements non bloquants, à corriger seulement si simple — "
                "ex. `target: x ?? null`) :\n" + "\n".join(dict.fromkeys(warnings)))[:TOOL_OUTPUT_LIMIT]
    return "quickshell ok"
