"""Fine-tuning dataset: examples validated by hand (via /keep or the bench candidate review).

Format of a data/dataset.jsonl line:
- request    : the request
- messages   : simple version (request → final files), Unsloth / TRL compatible
- trajectory : the agent's full conversation (tool calls and results) to learn how to use them
- files      : final content of the files created or modified; answer: final text answer
- meta       : origin (ui / bench), case, first try, number of calls…
"""

import json
import time

from .agent import Result
from .config import DATASET


def written_files(result: Result) -> dict[str, str]:
    """Final content of every file written or modified by the agent during the session."""
    paths: list[str] = []
    for m in result.messages:
        for call in m.get("tool_calls", []):
            f = call["function"]
            if f["name"] in ("write_file", "edit_file", "migrate_hyprlang") \
                    and f["arguments"].get("path") not in paths:
                paths.append(f["arguments"].get("path", ""))
    paths += [p for p in getattr(result, "changed", []) if p not in paths]
    return {p: (result.workspace / p).read_text() for p in paths if p and (result.workspace / p).is_file()}


def _simple(request: str, files: dict[str, str], answer: str) -> list[dict]:
    body = "\n\n".join(f"### {path}\n```{path.rsplit('.', 1)[-1]}\n{content.rstrip()}\n```"
                       for path, content in files.items())
    return [{"role": "user", "content": request}, {"role": "assistant", "content": body or answer}]


def add_entry(request: str, messages: list[dict], files: dict[str, str], answer: str, meta: dict):
    DATASET.parent.mkdir(parents=True, exist_ok=True)
    entry = {"request": request, "messages": _simple(request, files, answer), "trajectory": messages,
             "files": files, "answer": answer,
             "meta": {"date": time.strftime("%Y-%m-%d"), **meta, "files": list(files)}}
    with DATASET.open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def add(result: Result) -> None:
    """From the UI (/keep)."""
    files = written_files(result)
    add_entry(result.request, result.messages, files, result.summary,
              {"source": f"ui:{result.workspace.name}", "duration": round(result.duration, 1)})


def rows() -> list[dict]:
    if not DATASET.exists():
        return []
    return [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]


def remove(index: int) -> dict:
    """Removes example number index (1 = first). Returns the removed example."""
    all_rows = rows()
    removed = all_rows.pop(index - 1)
    DATASET.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in all_rows))
    return removed


def stats() -> str:
    all_rows = rows()
    if not all_rows:
        return "Dataset vide (0 exemple). Objectif avant fine-tuning : 300 à 1000."
    kinds: dict[str, int] = {}
    for r in all_rows:
        tops = {p.split("/", 1)[0] for p in r["meta"].get("files", [])} or {"question"}
        for top in tops:
            kinds[top] = kinds.get(top, 0) + 1
    with_traj = sum(1 for r in all_rows if r.get("trajectory"))
    detail = ", ".join(f"{k}: {v}" for k, v in sorted(kinds.items()))
    return (f"{len(all_rows)} exemples ({detail}) · {with_traj} avec trajectoire d'outils. "
            "Objectif avant fine-tuning : 300 à 1000.")
