"""Step 3 — the agent loop: generate → validate → fix → review.

`Session` keeps the conversation across requests and emits events (streamed text,
tool calls, results) that the CLI or the UI display however they like.
"""

import json
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path


from . import binds, rag, system
from .llm import TRANSIENT, client
from .config import (CHAT_MODEL, MAX_FIX_ROUNDS, MAX_STEPS, NUM_CTX, OLLAMA_RETRIES, SESSIONS_DIR,
                     SYSTEM_TOKENS, TOOL_OUTPUT_LIMIT)
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
    changed: list[str] = field(default_factory=list)   # files changed during this turn


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
        machine = ""
        if not self.messages and not os.environ.get("MYHYPR_NO_SYSTEM"):
            try:
                machine = f"Système détecté (détails : outil system_info) :\n{system.summary()}\n\n"
            except Exception:  # best-effort detection: never blocking
                machine = ""
        return {"role": "user", "content": (
            f"DEMANDE : {request}\n\n{machine}"
            f"Fichiers présents dans le dossier de travail :\n{self.workspace.list_files()}\n\n"
            f"Extraits de documentation pertinents :\n{context}")}

    def _chat(self) -> Iterator[Event]:
        """One streaming model call; appends the assistant message to the history."""
        if self._compact():
            yield Event("status", "contexte compacté", name="compact")
        for attempt in range(OLLAMA_RETRIES + 1):
            yield Event("status", name="model")
            content, thinking, calls, done_reason = "", "", [], ""
            try:
                stream = client.chat(model=self.model, messages=self.messages,
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
                        done_reason = chunk.done_reason or ""
                break
            except TRANSIENT as e:
                if attempt == OLLAMA_RETRIES:
                    raise RuntimeError(f"le modèle ne répond plus ({type(e).__name__}) après "
                                       f"{OLLAMA_RETRIES + 1} tentatives") from e
                yield Event("status", f"le modèle ne répondait plus ({type(e).__name__}) → nouvelle tentative",
                            name="retry", args={"attempt": attempt + 1})
        entry = {"role": "assistant", "content": content}
        if calls:
            entry["tool_calls"] = [{"function": {"name": c.function.name, "arguments": dict(c.function.arguments)}}
                                   for c in calls]
        self.messages.append(entry)
        if done_reason == "length":
            yield Event("status", "réponse coupée : limite de contexte atteinte", name="truncated")

    def _estimate(self) -> int:
        chars = sum(len(m.get("content", "")) + len(json.dumps(m.get("tool_calls", ""))) for m in self.messages)
        return SYSTEM_TOKENS + chars // 3

    def _compact(self) -> bool:
        """Beyond 75% of the context, Ollama would SILENTLY truncate the input (start of conversation lost).
        First replace old tool results and doc excerpts with a short note,
        keeping the last 8 messages intact."""
        if self._estimate() < 0.75 * NUM_CTX:
            return False
        changed = False
        for m in self.messages[:-8]:
            if self._estimate() < 0.6 * NUM_CTX:
                break
            content = m.get("content", "")
            if m["role"] == "tool" and m.get("tool_name") in ("read_file", "search_docs", "list_files") \
                    and len(content) > 300:
                m["content"] = f"[résultat de {m['tool_name']} retiré pour libérer le contexte — relance l'outil si besoin]"
                changed = True
            elif m["role"] == "user" and content.startswith("DEMANDE :") and "Extraits de documentation" in content:
                m["content"] = content.split("Extraits de documentation", 1)[0] + "[extraits de doc retirés]"
                changed = True
        return changed

    def turn(self, request: str) -> Iterator[Event]:
        start, first = time.time(), len(self.messages)
        tools = self.workspace.tools()
        self.messages.append(self._user_message(request))
        before = self._snapshot()
        conflicts_before = self._conflict_keys()
        entry = self.workspace.root / "hypr" / "hyprland.lua"
        binds_before = {(b.key, b.action) for b in binds.from_config(entry)} if entry.exists() else set()
        conflict_reported = False

        def finish(ok: bool, errors: list[str], summary: str) -> Event:
            now = self._snapshot()
            changed = sorted(f for f in now if before.get(f) != now[f])
            self.last = Result(request, self.workspace.root, ok, errors, summary,
                               self.messages[first:], time.time() - start, changed)
            return Event("done", result=self.last)

        fix_rounds, reviewed, nudged, summary = 0, False, False, ""
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
                    except Exception as e:  # the model must see the error to fix itself
                        out = f"ERREUR : {type(e).__name__}: {e}"
                    out = str(out)[:TOOL_OUTPUT_LIMIT]
                    yield Event("tool_result", out, name=name)
                    self.messages.append({"role": "tool", "tool_name": name, "content": out})
                continue

            # The model thinks it's done: check for ourselves
            summary = msg["content"]
            if self._snapshot() == before:
                attempted = any(c["function"]["name"] in ("write_file", "edit_file")
                                for m in self.messages[first:] for c in m.get("tool_calls", []))
                if attempted and not nudged:
                    # All its edits were rejected or reverted, but it thinks it succeeded
                    nudged = True
                    self.messages.append({"role": "user", "content":
                                          "Attention : aucun fichier n'a été modifié pendant ce tour (tes modifications "
                                          "ont été refusées ou annulées). La demande n'est donc PAS réalisée. Relis le "
                                          "passage avec read_file et refais une modification qui passe, ou explique "
                                          "honnêtement pourquoi tu n'y arrives pas."})
                    continue
                # Question, description, explanation: nothing changed → the reply is the answer
                yield finish(True, [], summary)
                return
            changed_now = self._changed(before)
            errors = self.workspace.validate_all(changed_now)
            if not errors and not conflict_reported:
                new = {k: v for k, v in self._conflicts().items() if k not in conflicts_before}
                if new:
                    # Valid for Hyprland, but one of the two actions will never fire
                    conflict_reported = True
                    yield Event("status", "nouveau conflit de raccourcis", name="conflict",
                                args={"lines": binds.describe(new)})
                    self.messages.append({"role": "user", "content": self._conflict_message(new, binds_before)})
                    continue
            if not errors:
                migrated = any(c["function"]["name"] == "migrate_hyprlang"
                               for m in self.messages[first:] for c in m.get("tool_calls", []))
                if reviewed or migrated:
                    # Migration: deterministic conversion + validation are enough; a review pushes the model
                    # to rewrite correct code (observed: 8 useless edits, all reverted)
                    break
                # Valid ≠ compliant: one review pass against the request (frequent omissions on a 9B)
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
            # Step limit: judge the final state, not the path (a valid config stays valid)
            errors = self.workspace.validate_all(self._changed(before))
            yield finish(not errors, errors, summary or "(limite d'étapes atteinte)")
            return

        errors = self.workspace.validate_all(self._changed(before))
        yield finish(not errors, errors, summary)

    @staticmethod
    def _conflict_message(new: dict, binds_before: set) -> str:
        """Points precisely at the old and new binds, with the exact line to disable."""
        parts = ["Ta modification crée un conflit de raccourcis : une même combinaison déclenche deux actions "
                 "différentes, une seule fonctionnera."]
        for (_, combo, _), group in new.items():
            old = [b for b in group if (b.key, b.action) in binds_before]
            fresh = [b for b in group if (b.key, b.action) not in binds_before]
            parts.append(f"\n{combo} :")
            for b in old:
                parts.append(f"  ANCIEN  {b.action} @ {b.where} : {b.text}")
            for b in fresh:
                parts.append(f"  NOUVEAU {b.action} @ {b.where} : {b.text}")
        parts.append("\nSi la demande nomme explicitement cette combinaison, le nouveau raccourci gagne : commente "
                     "l'ANCIEN avec edit_file (old = sa ligne exacte ci-dessus, new = « -- » + la même ligne) et dis "
                     "à l'utilisateur quelle action n'a plus de raccourci. Sinon, choisis pour le NOUVEAU une "
                     "combinaison libre (check_binds avec keys). N'ajoute pas de nouveau hl.bind en double.")
        return "\n".join(parts)

    def _conflicts(self) -> dict:
        entry = self.workspace.root / "hypr" / "hyprland.lua"
        return binds.conflicts(binds.from_config(entry)) if entry.exists() else {}

    def _conflict_keys(self) -> set:
        return set(self._conflicts())

    def _changed(self, before: dict[str, int]) -> set[str]:
        now = self._snapshot()
        return {f for f in now if before.get(f) != now[f]}

    def _snapshot(self) -> dict[str, int]:
        root = self.workspace.root
        return {p.relative_to(root).as_posix(): hash(p.read_bytes()) for p in root.rglob("*") if p.is_file()}

    def cancel_pending(self):
        """After a Ctrl+C: drop a tool call left unanswered to keep the history consistent."""
        while self.messages and self.messages[-1]["role"] == "assistant" and self.messages[-1].get("tool_calls"):
            self.messages.pop()


def run(request: str, workspace: Workspace, *, think: bool = False, verbose: bool = True,
        model: str = CHAT_MODEL) -> Result:
    """Non-interactive mode (myhypr ask)."""
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
