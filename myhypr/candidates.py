"""Candidats pour le dataset : sorties conformes des runs du banc, à valider à la main avant d'entrer au dataset.

Chaque candidat est RE-NOTÉ avec les vérifications actuelles (les anciens scores ont pu être faussés par des
vérifications corrigées depuis) et sa config Hyprland est revalidée. On garde les trajectoires les plus propres
(réussies du 1er coup, peu d'appels), au plus MAX_PER_CASE par demande, sans doublons de contenu.
"""

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import DATA
from .evaluate import EVALS, RESULTS, load_cases, run_checks, snapshot

STATE = DATA / "candidates.json"
MAX_PER_CASE = 3


@dataclass
class Candidate:
    id: str
    case: str
    request: str
    seed: str | None
    files: dict[str, str]          # fichiers créés ou modifiés (contenu final)
    original: dict[str, str]       # contenu d'origine des fichiers modifiés (cas « seed »)
    answer: str
    messages: list[dict] = field(default_factory=list)
    first_try: bool = False
    model_calls: int = 0
    seconds: float = 0.0
    source: str = ""


def _state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1))


def harvest() -> list[Candidate]:
    """Tous les candidats valables (avant filtrage par décision et plafond par demande)."""
    from .tools import verify_hyprland_file
    cases = {c["id"]: c for c in load_cases(None, None)}
    found: dict[str, Candidate] = {}
    for result in sorted(RESULTS.glob("*.json")):
        if "rescored" in result.name:
            continue
        data = json.loads(result.read_text())
        for run in data["runs"]:
            case = cases.get(run["id"])
            ws = Path(run["workspace"])
            session = ws.parent / f"{run['id']}.session.json"
            if not case or not run.get("valid") or not ws.is_dir() or not session.exists():
                continue
            before = snapshot(EVALS / "fixtures" / case["seed"]) if case.get("seed") else {}
            after = snapshot(ws)
            changed = sorted(f for f in after if before.get(f) != after[f])
            messages = json.loads(session.read_text())
            answer = next((m["content"] for m in reversed(messages) if m["role"] == "assistant" and m["content"]), "")
            if not all(r.ok for r in run_checks(case, ws, changed, answer)):   # vérifications ACTUELLES
                continue
            entry = ws / "hypr" / "hyprland.lua"
            if any(f.startswith("hypr/") and f.endswith(".lua") for f in changed) and entry.exists() \
                    and verify_hyprland_file(entry) != "config ok":
                continue
            files = {f: after[f] for f in changed}
            if not files and not case["id"].startswith("q-"):
                continue
            key = hashlib.sha1(json.dumps([case["request"], files, answer if not files else ""],
                                          sort_keys=True).encode()).hexdigest()[:12]
            if key in found:
                continue
            found[key] = Candidate(
                id=key, case=case["id"], request=case["request"], seed=case.get("seed"), files=files,
                original={f: before[f] for f in changed if f in before}, answer=answer, messages=messages,
                first_try=run.get("first_try", False), model_calls=run.get("model_calls", 0),
                seconds=run.get("seconds", 0.0), source=f"{data['date']}/{run['id']}")
    return list(found.values())


def pending() -> list[Candidate]:
    """Candidats à examiner : les plus propres d'abord, MAX_PER_CASE par demande (acceptés compris)."""
    state = _state()
    accepted = {}
    for v in state.values():
        if v["status"] == "accepted":
            accepted[v["case"]] = accepted.get(v["case"], 0) + 1
    out, per_case = [], dict(accepted)
    for c in sorted(harvest(), key=lambda c: (c.case, not c.first_try, c.model_calls, c.seconds)):
        if c.id in state or per_case.get(c.case, 0) >= MAX_PER_CASE:
            continue
        per_case[c.case] = per_case.get(c.case, 0) + 1
        out.append(c)
    return out


def decide(c: Candidate, accept: bool):
    from . import dataset
    state = _state()
    state[c.id] = {"status": "accepted" if accept else "rejected", "case": c.case, "source": c.source,
                   "date": time.strftime("%Y-%m-%d %H:%M")}
    _save_state(state)
    if accept:
        dataset.add_entry(request=c.request, messages=c.messages, files=c.files, answer=c.answer,
                          meta={"source": f"banc:{c.source}", "case": c.case, "first_try": c.first_try,
                                "model_calls": c.model_calls, "seed": c.seed})


def stats() -> str:
    state = _state()
    acc = sum(1 for v in state.values() if v["status"] == "accepted")
    rej = sum(1 for v in state.values() if v["status"] == "rejected")
    pend = pending()
    return (f"{len(pend)} candidat(s) à examiner ({len({c.case for c in pend})} demandes) · "
            f"{acc} accepté(s) · {rej} rejeté(s)")
