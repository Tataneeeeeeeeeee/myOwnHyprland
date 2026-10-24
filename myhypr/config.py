"""Chemins et réglages du projet."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS_RAW = ROOT / "docs" / "raw"
DATA = ROOT / "data"
INDEX_DIR = DATA / "index"
SESSIONS_DIR = DATA / "sessions"
BACKUPS_DIR = DATA / "backups"
DATASET = DATA / "dataset.jsonl"
WORKSPACES = ROOT / "workspaces"

CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))

CHAT_MODEL = os.environ.get("MYHYPR_MODEL", "hypr-agent")
EMBED_MODEL = os.environ.get("MYHYPR_EMBED", "nomic-embed-text")

MAX_STEPS = 25          # appels au modèle max par demande
MAX_FIX_ROUNDS = 4      # relances automatiques si la validation échoue
TOOL_OUTPUT_LIMIT = 6000
NUM_CTX = 32768         # doit correspondre à PARAMETER num_ctx du Modelfile
SYSTEM_TOKENS = 1700    # taille approximative du prompt système
