"""Opérations sur ~/.config partagées par la CLI et l'UI : apply, restore, diff."""

import difflib
import shutil
import time
from pathlib import Path

from .config import BACKUPS_DIR, CONFIG_HOME
from .tools import Workspace


def seed(ws: Workspace, names: list[str]) -> list[str]:
    """Copie des dossiers de ~/.config dans le dossier de travail. Renvoie ceux copiés."""
    done = []
    for name in names:
        src = CONFIG_HOME / name
        if src.is_dir():
            shutil.copytree(src, ws.root / name, symlinks=False, dirs_exist_ok=True)
            done.append(name)
    return done


def apply(ws: Workspace) -> Path:
    """Copie les fichiers du dossier de travail dans ~/.config après sauvegarde. Renvoie la sauvegarde."""
    files = ws.files()
    backup = BACKUPS_DIR / time.strftime("%Y%m%d-%H%M%S")
    for top in sorted({f.split("/", 1)[0] for f in files}):
        if (CONFIG_HOME / top).exists():
            shutil.copytree(CONFIG_HOME / top, backup / top, symlinks=True)
    for f in files:
        dst = CONFIG_HOME / f
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ws.root / f, dst)
    return backup


def backups() -> list[str]:
    return sorted(p.name for p in BACKUPS_DIR.iterdir()) if BACKUPS_DIR.exists() else []


def restore(name: str) -> list[str]:
    backup = BACKUPS_DIR / name
    if not backup.is_dir():
        raise FileNotFoundError(f"Sauvegarde introuvable : {backup}")
    restored = []
    for top in sorted(p.name for p in backup.iterdir()):
        dst = CONFIG_HOME / top
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(backup / top, dst, symlinks=True)
        restored.append(top)
    return restored


def diff(ws: Workspace, only: str | None = None) -> str:
    """Diff unifié entre ~/.config et le dossier de travail."""
    out = []
    for f in ws.files():
        if only and not f.startswith(only):
            continue
        cur = CONFIG_HOME / f
        old = cur.read_text(errors="ignore").splitlines(keepends=True) if cur.exists() else []
        new = (ws.root / f).read_text(errors="ignore").splitlines(keepends=True)
        out += difflib.unified_diff(old, new, f"~/.config/{f}", f"workspace/{f}")
    return "".join(out)
