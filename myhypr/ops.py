"""Operations on ~/.config shared by the CLI and the UI: apply, restore, diff."""

import difflib
import json
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .config import BACKUPS_DIR, CONFIG_HOME
from .tools import Workspace


def seed(ws: Workspace, names: list[str]) -> list[str]:
    """Copies directories from ~/.config into the working directory. Returns those copied."""
    done = []
    for name in names:
        src = CONFIG_HOME / name
        if src.is_dir():
            shutil.copytree(src, ws.root / name, symlinks=False, dirs_exist_ok=True)
            done.append(name)
    return done


def apply(ws: Workspace) -> Path:
    """Copies the working directory's files into ~/.config after a backup. Returns the backup."""
    files = ws.files()
    backup = BACKUPS_DIR / time.strftime("%Y%m%d-%H%M%S")
    n = 1
    while backup.exists():  # two applies within the same second
        n += 1
        backup = BACKUPS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{n}"
    backup.mkdir(parents=True)
    absent = []
    for top in sorted({f.split("/", 1)[0] for f in files}):
        if (CONFIG_HOME / top).exists():
            shutil.copytree(CONFIG_HOME / top, backup / top, symlinks=True)
        else:
            absent.append(top)  # created by /apply: delete on restore
    (backup / ABSENT).write_text(json.dumps(absent))
    for f in files:
        dst = CONFIG_HOME / f
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ws.root / f, dst)
    return backup


ABSENT = ".absent.json"  # ~/.config directories that didn't exist before /apply


def backups() -> list[str]:
    return sorted(p.name for p in BACKUPS_DIR.iterdir()) if BACKUPS_DIR.exists() else []


def restore(name: str) -> list[str]:
    backup = BACKUPS_DIR / name
    if not backup.is_dir():
        raise FileNotFoundError(f"Sauvegarde introuvable : {backup}")
    restored = []
    marker = backup / ABSENT
    for top in json.loads(marker.read_text()) if marker.exists() else []:
        created = CONFIG_HOME / top
        if created.is_symlink():
            created.unlink()
        elif created.exists():
            shutil.rmtree(created)
        restored.append(f"{top} (supprimé, n'existait pas avant)")
    for top in sorted(p.name for p in backup.iterdir() if p.is_dir()):
        # ~/.config/hypr is often a link to a dotfiles repo (ml4w, stow…): keep the link
        # and replace the contents of the REAL directory (rmtree refuses a symlink)
        dst = (CONFIG_HOME / top).resolve()
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(backup / top, dst, symlinks=True)
        restored.append(top)
    return restored


def diff(ws: Workspace, only: str | None = None) -> str:
    """Unified diff between ~/.config and the working directory."""
    out = []
    for f in ws.files():
        if only and not f.startswith(only):
            continue
        cur = CONFIG_HOME / f
        old = cur.read_text(errors="ignore").splitlines(keepends=True) if cur.exists() else []
        new = (ws.root / f).read_text(errors="ignore").splitlines(keepends=True)
        out += difflib.unified_diff(old, new, f"~/.config/{f}", f"workspace/{f}")
    return "".join(out)


# --------------------------------------------------------------------------- live apply

def hypr_errors() -> list[str]:
    """Errors in the config loaded by the running Hyprland session (empty if all is well)."""
    try:
        r = subprocess.run(["hyprctl", "configerrors", "-j"], capture_output=True, text=True, timeout=5)
        return [e.strip() for e in json.loads(r.stdout) if e.strip()]
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return []


def reload_hypr() -> str:
    try:
        return subprocess.run(["hyprctl", "reload"], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"échec : {e}"


def waybar_instances() -> list[tuple[int, str]]:
    """(pid, config file) of running Waybars; '' if started without -c (= ~/.config/waybar)."""
    out = subprocess.run(["pgrep", "-a", "waybar"], capture_output=True, text=True).stdout
    found = []
    for line in out.splitlines():
        pid, _, cmd = line.partition(" ")
        if not cmd.split() or Path(cmd.split()[0]).name != "waybar":
            continue
        args = cmd.split()
        cfg = args[args.index("-c") + 1] if "-c" in args[:-1] else args[args.index("--config") + 1] \
            if "--config" in args[:-1] else ""
        found.append((int(pid), cfg))
    return found


def reload_waybar(pids: list[int]):
    for pid in pids:
        try:
            os.kill(pid, signal.SIGUSR2)  # Waybar reloads config + style without restarting
        except ProcessLookupError:
            pass


@dataclass
class LiveReport:
    backup: Path
    steps: list[tuple[str, str]] = field(default_factory=list)   # (level ok|info|warn|err, message)
    rolled_back: bool = False

    def add(self, level: str, msg: str):
        self.steps.append((level, msg))


def apply_live(ws: Workspace, keep: Callable[[], bool], wait: float = 1.5) -> LiveReport:
    """Applies to ~/.config, hot-reloads, checks, and restores the backup if something breaks
    or the user doesn't confirm (`keep` returns False, e.g. timeout)."""
    files = ws.files()
    tops = {f.split("/", 1)[0] for f in files}
    baseline = hypr_errors() if "hypr" in tops else []
    report = LiveReport(apply(ws))
    report.add("ok", f"{len(files)} fichier(s) copiés · sauvegarde {report.backup.name}")

    def rollback(reason: str):
        restore(report.backup.name)
        if "hypr" in tops:
            reload_hypr()
        reload_waybar(waybar_pids)
        report.rolled_back = True
        report.add("warn", f"{reason} → sauvegarde {report.backup.name} restaurée")

    waybar_pids: list[int] = []
    if "hypr" in tops:
        report.add("info", f"hyprctl reload : {reload_hypr() or 'ok'}")
        time.sleep(wait)
        new = [e for e in hypr_errors() if e not in baseline]
        if new:
            for e in new[:5]:
                report.add("err", e)
            rollback("Hyprland signale des erreurs après rechargement")
            return report
        report.add("ok", "Hyprland rechargé sans nouvelle erreur"
                   + (f" ({len(baseline)} erreur(s) déjà présentes avant, inchangées)" if baseline else ""))

    if "waybar" in tops:
        ours = (CONFIG_HOME / "waybar").resolve()
        instances = waybar_instances()
        waybar_pids = [pid for pid, cfg in instances if not cfg or Path(cfg).expanduser().resolve().is_relative_to(ours)]
        others = [cfg for pid, cfg in instances if pid not in waybar_pids]
        if waybar_pids:
            reload_waybar(waybar_pids)
            time.sleep(wait)
            alive = {pid for pid, _ in waybar_instances()}
            if not alive.issuperset(waybar_pids):
                rollback("Waybar s'est arrêtée après rechargement")
                return report
            report.add("ok", "Waybar rechargée")
        elif others:
            report.add("warn", f"ta Waybar active utilise {others[0]} et non ~/.config/waybar : "
                               "les fichiers appliqués ne seront pas visibles tant qu'elle n'est pas lancée dessus")
        else:
            report.add("info", "aucune Waybar en cours : lance-la avec  waybar &")

    hints = {"hypr/hypridle.conf": "hypridle : relance-le pour prendre la config  →  pkill hypridle; hypridle & disown",
             "hypr/hyprpaper.conf": "hyprpaper : relance-le  →  pkill hyprpaper; hyprpaper & disown "
                                    "(arrête d'abord awww/swww s'ils gèrent déjà le fond)",
             "hypr/hyprlock.conf": "hyprlock : sera utilisé au prochain verrouillage ; pour tester  →  hyprlock "
                                   "(déverrouille avec ton mot de passe)"}
    for f, hint in hints.items():
        if f in files:
            report.add("info", hint)
    if any(f.startswith("matugen/") for f in files):
        report.add("info", "matugen : régénère les couleurs avec  matugen image <ton fond> --prefer saturation  "
                           "(lance aussi les hooks de ta config, ex. rechargement de kitty)")

    for name in sorted({f.split("/")[1] for f in files if f.startswith("quickshell/") and f.count("/") >= 2}):
        report.add("info", f"Quickshell : qs -p ~/.config/quickshell/{name}")

    if not keep():
        rollback("changements non confirmés")
    else:
        report.add("ok", "changements conservés")
    return report
