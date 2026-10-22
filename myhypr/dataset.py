"""Étape 4 — collecte du dataset de fine-tuning à partir des sessions réussies."""

import json
import time

from .agent import Result
from .config import DATASET


def written_files(result: Result) -> dict[str, str]:
    """Contenu final de chaque fichier écrit par l'agent pendant la session."""
    files: dict[str, str] = {}
    for m in result.messages:
        for call in m.get("tool_calls", []):
            f = call["function"]
            if f["name"] == "write_file":
                files[f["arguments"]["path"]] = f["arguments"]["content"]
    return files


def to_example(result: Result) -> dict:
    """Format chat (messages) compatible Unsloth / TRL : demande → fichiers finaux."""
    files = written_files(result)
    answer = "\n\n".join(f"### {path}\n```{path.rsplit('.', 1)[-1]}\n{content.rstrip()}\n```"
                         for path, content in files.items())
    return {
        "messages": [
            {"role": "user", "content": result.request},
            {"role": "assistant", "content": answer},
        ],
        "meta": {"workspace": result.workspace.name, "date": time.strftime("%Y-%m-%d"),
                 "duration": round(result.duration, 1), "files": list(files)},
    }


def add(result: Result) -> None:
    DATASET.parent.mkdir(parents=True, exist_ok=True)
    with DATASET.open("a") as f:
        f.write(json.dumps(to_example(result), ensure_ascii=False) + "\n")


def stats() -> str:
    if not DATASET.exists():
        return "Dataset vide (0 exemple). Objectif avant fine-tuning : 300 à 1000."
    rows = [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]
    kinds: dict[str, int] = {}
    for r in rows:
        for path in r["meta"]["files"]:
            top = path.split("/", 1)[0]
            kinds[top] = kinds.get(top, 0) + 1
    detail = ", ".join(f"{k}: {v}" for k, v in sorted(kinds.items()))
    return f"{len(rows)} exemples ({detail}). Objectif avant fine-tuning : 300 à 1000."
