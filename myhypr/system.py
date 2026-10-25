"""Détection du système : écrans, périphériques, GPU, logiciels installés, version et format de config.

Le résumé est injecté dans la première demande de chaque session pour que l'agent écrive une config adaptée
à la machine (bon nom d'écran, bonne disposition clavier, variables NVIDIA si besoin, logiciels réellement installés).
"""

import json
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from .config import CONFIG_HOME

# Catégorie → binaires candidats (le premier trouvé est cité en premier)
SOFTWARE = {
    "terminal": ["kitty", "alacritty", "foot", "wezterm", "ghostty", "konsole", "gnome-terminal"],
    "lanceur": ["rofi", "wofi", "fuzzel", "walker", "anyrun", "tofi", "hyprlauncher"],
    "barre / widgets": ["waybar", "qs", "eww", "ags", "hyprpanel", "nwg-panel"],
    "gestionnaire de fichiers": ["dolphin", "nautilus", "thunar", "nemo", "pcmanfm-qt", "yazi"],
    "notifications": ["swaync", "mako", "dunst", "fnott"],
    "fond d'écran": ["hyprpaper", "swww", "awww", "swaybg", "mpvpaper", "waypaper"],
    "verrouillage / veille": ["hyprlock", "hypridle", "swaylock", "swayidle"],
    "capture d'écran": ["hyprshot", "grim", "slurp", "grimblast", "satty", "swappy"],
    "presse-papiers": ["cliphist", "wl-copy", "copyq"],
    "audio": ["wpctl", "pamixer", "pavucontrol", "pactl"],
    "luminosité": ["brightnessctl", "light"],
    "média": ["playerctl"],
    "réseau": ["nm-applet", "nmtui", "iwgtk"],
    "polkit": ["hyprpolkitagent", "lxpolkit", "polkit-kde-authentication-agent-1"],
    "thème depuis le fond d'écran": ["matugen", "wal", "wallust"],
    "plugins Hyprland": ["hyprpm"],
    "écosystème Hypr": ["hyprpicker", "hyprsunset", "hyprctl"],
}
# Agents polkit souvent installés hors du PATH
POLKIT_PATHS = ["/usr/lib/hyprpolkitagent/hyprpolkitagent", "/usr/lib/polkit-gnome/polkit-gnome-authentication-agent-1",
                "/usr/libexec/polkit-gnome-authentication-agent-1", "/usr/lib/polkit-kde-authentication-agent-1"]


def _run_json(*cmd: str):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def _gpus() -> list[dict]:
    try:
        out = subprocess.run(["lspci"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    gpus = []
    for line in out.splitlines():
        if re.search(r"VGA compatible|3D controller|Display controller", line):
            name = line.split(": ", 1)[-1]
            vendor = "nvidia" if "NVIDIA" in name else "amd" if re.search(r"AMD|ATI|Radeon", name) else \
                "intel" if "Intel" in name else "autre"
            gpus.append({"vendor": vendor, "name": name})
    return gpus


def _nvidia_driver() -> str | None:
    p = Path("/proc/driver/nvidia/version")
    if p.exists():
        m = re.search(r"Kernel Module\s+(?:for \S+\s+)?([\d.]+)", p.read_text())
        return m.group(1) if m else "chargé"
    return None


@lru_cache(maxsize=1)
def detect() -> dict:
    """Instantané de la machine (mis en cache pour la durée du processus)."""
    info: dict = {}
    v = _run_json("hyprctl", "version", "-j") or {}
    hypr_dir = CONFIG_HOME / "hypr"
    info["hyprland"] = {
        "version": v.get("version") or v.get("tag", "?"),
        "format_config": "lua" if (hypr_dir / "hyprland.lua").exists()
        else "hyprlang (.conf)" if (hypr_dir / "hyprland.conf").exists() else "aucune config",
        "hyprland.conf_aussi_present": (hypr_dir / "hyprland.conf").exists() and (hypr_dir / "hyprland.lua").exists(),
    }

    info["ecrans"] = [{
        "nom": m["name"], "description": m.get("description", ""),
        "mode": f"{m['width']}x{m['height']}@{m['refreshRate']:.2f}",
        "position": f"{m['x']}x{m['y']}", "echelle": m.get("scale"), "rotation": m.get("transform"),
        "vrr": m.get("vrr"), "modes_dispo": m.get("availableModes", [])[:6],
    } for m in _run_json("hyprctl", "monitors", "all", "-j") or []]

    dev = _run_json("hyprctl", "devices", "-j") or {}
    keyboards = dev.get("keyboards", [])
    main = next((k for k in keyboards if k.get("main")), keyboards[0] if keyboards else {})
    mice = [m["name"] for m in dev.get("mice", []) if "virtual" not in m["name"]]
    info["entrees"] = {
        "clavier_principal": main.get("name"), "disposition": main.get("layout"), "variante": main.get("variant"),
        "options": main.get("options"),
        "souris": [m for m in mice if "touchpad" not in m],
        "touchpad": [m for m in mice if "touchpad" in m],
        "tablettes": len(dev.get("tablets", [])), "ecrans_tactiles": len(dev.get("touch", [])),
    }

    gpus = _gpus()
    info["gpu"] = {"cartes": gpus, "pilote_nvidia": _nvidia_driver(),
                   "hybride": len({g["vendor"] for g in gpus}) > 1}

    found = {}
    for cat, bins in SOFTWARE.items():
        hits = [b for b in bins if shutil.which(b)]
        if cat == "polkit":
            hits += [Path(p).name for p in POLKIT_PATHS if Path(p).exists() and Path(p).name not in hits]
        if hits:
            found[cat] = hits
    info["logiciels"] = found
    try:
        procs = subprocess.run(["ps", "-eo", "comm="], capture_output=True, text=True, timeout=5).stdout.split()
    except (OSError, subprocess.TimeoutExpired):
        procs = []
    watched = ["waybar", "quickshell", "qs", "eww", "ags", "hyprpanel", "swaync", "mako", "dunst", "hyprpaper",
               "swww-daemon", "awww-daemon", "swaybg", "mpvpaper", "hypridle", "swayidle", "hyprsunset", "cliphist",
               "hyprpolkitagent", "nm-applet"]
    info["en_cours"] = [w for w in watched if w in procs]
    from .palette import current_wallpaper, matugen_outputs
    info["fond_ecran"] = current_wallpaper()
    info["genere_par_matugen"] = matugen_outputs()
    info["raccourcis_actifs"] = len(_run_json("hyprctl", "binds", "-j") or [])
    return info


def summary(info: dict | None = None) -> str:
    """Résumé compact (≈ 300 tokens) pour le contexte de l'agent."""
    info = info or detect()
    h, e, g = info["hyprland"], info["entrees"], info["gpu"]
    lines = [f"Hyprland {h['version']} · config actuelle : {h['format_config']}"
             + (" (hyprland.conf aussi présent, ignoré car hyprland.lua est prioritaire)"
                if h["hyprland.conf_aussi_present"] else "")
             + (" → ancien format : propose la migration vers Lua (outil migrate_hyprlang)"
                if h["format_config"].startswith("hyprlang") else "")]
    for m in info["ecrans"]:
        lines.append(f"Écran {m['nom']} ({m['description']}) : {m['mode']} Hz, position {m['position']}, "
                     f"échelle {m['echelle']}, rotation {m['rotation']}, VRR {'oui' if m['vrr'] else 'non'}")
    kb = f"{e['disposition'] or '?'}" + (f" variante {e['variante']}" if e["variante"] else "")
    lines.append(f"Clavier : {kb} ({e['clavier_principal']}) · souris : {', '.join(e['souris'][:3]) or 'aucune'}"
                 f" · touchpad : {'oui' if e['touchpad'] else 'non'}")
    cards = " + ".join(f"{c['vendor']}" for c in g["cartes"]) or "?"
    lines.append(f"GPU : {cards}" + (" (hybride)" if g["hybride"] else "")
                 + (f" · pilote NVIDIA {g['pilote_nvidia']} → variables d'environnement NVIDIA nécessaires"
                    if g["pilote_nvidia"] else ""))
    soft = "; ".join(f"{cat} : {', '.join(b)}" for cat, b in info["logiciels"].items())
    if info.get("fond_ecran"):
        lines.append(f"Fond d'écran actuel : {info['fond_ecran']} (palette : outil wallpaper_palette)")
    if info.get("genere_par_matugen"):
        lines.append("Fichiers GÉNÉRÉS par matugen (ne pas les modifier : modifier le template dans "
                     "~/.config/matugen/templates/) : " + ", ".join(info["genere_par_matugen"]))
    if info.get("en_cours"):
        lines.append(f"En cours d'exécution : {', '.join(info['en_cours'])}"
                     + (" (le fond d'écran n'est PAS géré par hyprpaper)" if "hyprpaper" not in info["en_cours"]
                        and any(d in info["en_cours"] for d in ("awww-daemon", "swww-daemon", "swaybg", "mpvpaper"))
                        else ""))
    lines.append(f"Installés : {soft}")
    lines.append("Utilise ces noms d'écran et ces logiciels réels plutôt que des valeurs génériques, "
                 "sauf si la demande dit autre chose.")
    return "\n".join(lines)
