"""Étape 3 — la boucle agent : générer → valider → corriger → relire.

`Session` garde la conversation entre les demandes et émet des événements (texte en streaming,
appels d'outils, résultats) que la CLI ou l'UI affichent comme elles veulent.
"""

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import ollama

from . import rag
from .config import CHAT_MODEL, MAX_FIX_ROUNDS, MAX_STEPS, SESSIONS_DIR, TOOL_OUTPUT_LIMIT
from .tools import Workspace


@dataclass
class Result:
    request: str
    workspace: Path
    ok: bool
    errors: list[str]
    summary: str
    messages: list[dict] = field(default_factory=list)
    duration: float = 0.0


@dataclass
class Event:
    # thinking | text | tool_call | tool_result | status | done
    kind: str
    text: str = ""
    name: str = ""
    args: dict = field(default_factory=dict)
    result: Result | None = None


class Session:
    def __init__(self, workspace: Workspace, *, model: str = CHAT_MODEL, think: bool = False):
        self.workspace = workspace
        self.model = model
        self.think = think
        self.messages: list[dict] = []
        self.last: Result | None = None
        self.tokens = 0

    def _user_message(self, request: str) -> dict:
        try:
            context = rag.search_mixed(request, docs=3, examples=2)
        except RuntimeError as e:
            context = f"(doc indisponible : {e})"
        return {"role": "user", "content": (
            f"DEMANDE : {request}\n\n"
            f"Fichiers présents dans le dossier de travail :\n{self.workspace.list_files()}\n\n"
            f"Extraits de documentation pertinents :\n{context}")}

    def _chat(self) -> Iterator[Event]:
        """Un appel au modèle en streaming ; ajoute le message assistant à l'historique."""
        content, thinking, calls = "", "", []
        yield Event("status", name="model")
        stream = ollama.chat(model=self.model, messages=self.messages,
                             tools=list(self.workspace.tools().values()), think=self.think, stream=True)
        for chunk in stream:
            m = chunk.message
            if m.thinking:
                thinking += m.thinking
                yield Event("thinking", m.thinking)
            if m.content:
                content += m.content
                yield Event("text", m.content)
            if m.tool_calls:
                calls += m.tool_calls
            if chunk.done:
                self.tokens = (chunk.prompt_eval_count or 0) + (chunk.eval_count or 0)
        entry = {"role": "assistant", "content": content}
        if calls:
            entry["tool_calls"] = [{"function": {"name": c.function.name, "arguments": dict(c.function.arguments)}}
                                   for c in calls]
        self.messages.append(entry)

    def turn(self, request: str) -> Iterator[Event]:
        start, first = time.time(), len(self.messages)
        tools = self.workspace.tools()
        self.messages.append(self._user_message(request))

        def finish(ok: bool, errors: list[str], summary: str) -> Event:
            self.last = Result(request, self.workspace.root, ok, errors, summary,
                               self.messages[first:], time.time() - start)
            return Event("done", result=self.last)

        fix_rounds, reviewed, summary = 0, False, ""
        for _ in range(MAX_STEPS):
            yield from self._chat()
            msg = self.messages[-1]

            if msg.get("tool_calls"):
                for call in msg["tool_calls"]:
                    name, args = call["function"]["name"], call["function"]["arguments"]
                    yield Event("tool_call", name=name, args=args)
                    fn = tools.get(name)
                    try:
                        out = fn(**args) if fn else f"ERREUR : outil inconnu {name}"
                    except Exception as e:  # le modèle doit voir l'erreur pour se corriger
                        out = f"ERREUR : {type(e).__name__}: {e}"
                    out = str(out)[:TOOL_OUTPUT_LIMIT]
                    yield Event("tool_result", out, name=name)
                    self.messages.append({"role": "tool", "tool_name": name, "content": out})
                continue

            # Le modèle pense avoir fini : on vérifie nous-mêmes
            summary = msg["content"]
            if not self.workspace.files():
                # Simple question / discussion : pas de fichier attendu
                yield finish(True, [], summary)
                return
            errors = self.workspace.validate_all()
            if not errors:
                if reviewed:
                    break
                # Valide ≠ conforme : une passe de relecture contre la demande (oublis fréquents sur un 9B)
                reviewed = True
                yield Event("status", "validation ok → relecture de la demande", name="review")
                self.messages.append({"role": "user", "content":
                                      "La validation passe. Relis maintenant la DEMANDE point par point et vérifie avec "
                                      "read_file que CHAQUE point est réellement présent dans les fichiers "
                                      "(position, couleurs exactes, modules, raccourcis…). Ne supprime rien qui sert "
                                      "la demande. Si un point manque ou est faux, corrige-le et revalide. "
                                      "Sinon, réponds par un résumé honnête de 2-3 lignes."})
                continue
            if fix_rounds >= MAX_FIX_ROUNDS:
                yield finish(False, errors, summary)
                return
            fix_rounds += 1
            yield Event("status", f"validation échouée → relance {fix_rounds}/{MAX_FIX_ROUNDS}", name="fix",
                        args={"round": fix_rounds, "max": MAX_FIX_ROUNDS})
            self.messages.append({"role": "user", "content":
                                  "La validation automatique a échoué. Corrige ces erreurs (search_docs si besoin) "
                                  "puis réécris les fichiers concernés :\n\n" + "\n\n".join(errors)})
        else:
            yield finish(False, self.workspace.validate_all() or ["nombre maximum d'étapes atteint"], summary)
            return

        errors = self.workspace.validate_all()
        yield finish(not errors, errors, summary)

    def cancel_pending(self):
        """Après un Ctrl+C : retire un appel d'outil resté sans réponse pour garder un historique cohérent."""
        while self.messages and self.messages[-1]["role"] == "assistant" and self.messages[-1].get("tool_calls"):
            self.messages.pop()


def run(request: str, workspace: Workspace, *, think: bool = False, verbose: bool = True,
        model: str = CHAT_MODEL) -> Result:
    """Mode non interactif (myhypr ask)."""
    session = Session(workspace, model=model, think=think)
    for ev in session.turn(request):
        if not verbose:
            continue
        if ev.kind == "tool_call":
            shown = {k: (v[:60] + "…" if isinstance(v, str) and len(v) > 60 else v) for k, v in ev.args.items()}
            print(f"  → {ev.name}({shown})", flush=True)
        elif ev.kind == "tool_result" and ev.name.startswith("verify"):
            print(f"    {ev.text.splitlines()[0] if ev.text else ''}", flush=True)
        elif ev.kind == "status":
            print(f"  · {ev.text}", flush=True)
    return session.last


def save_session(result: Result) -> Path:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    path = SESSIONS_DIR / f"{result.workspace.name}.json"
    path.write_text(json.dumps({
        "request": result.request,
        "workspace": str(result.workspace),
        "ok": result.ok,
        "errors": result.errors,
        "summary": result.summary,
        "duration": round(result.duration, 1),
        "messages": result.messages,
    }, ensure_ascii=False, indent=2))
    return path
