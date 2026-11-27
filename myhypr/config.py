"""Project paths and settings."""

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

MAX_STEPS = 25          # max model calls per request
MAX_FIX_ROUNDS = 4      # automatic retries when validation fails
TOOL_OUTPUT_LIMIT = 6000
NUM_CTX = 32768         # must match PARAMETER num_ctx in the Modelfile
SYSTEM_TOKENS = 1700    # approximate system prompt size

# Max delay between two chunks of the model's response (s): beyond it, the connection is considered dead
# (e.g. after a suspend) and the call is retried instead of waiting forever.
OLLAMA_READ_TIMEOUT = 300
OLLAMA_RETRIES = 2
