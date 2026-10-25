"""Banc d'évaluation : lance les cas de evals/cases.toml et mesure validité + conformité."""

import json
import os
import re
import shutil
import subprocess
import time
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.text import Text

from .agent import Session
from .config import CHAT_MODEL, ROOT
from .theme import C, RICH_THEME
from .tools import Workspace, _strip_jsonc

EVALS = ROOT / "evals"
RESULTS = EVALS / "results"
MOCK = ROOT / "myhypr" / "hl_mock.lua"
console = Console(theme=RICH_THEME, highlight=False)


# ----------------------------------------------------------------------------- extraction

def waybar_conf(ws: Path) -> dict | None:
    for name in ("waybar/config.jsonc", "waybar/config.json", "waybar/config"):
        p = ws / name
        if p.exists():
            try:
                conf = json.loads(_strip_jsonc(p.read_text()))
            except json.JSONDecodeError:
                return None
            return conf[0] if isinstance(conf, list) and conf else conf
    return None


def hypr_conf(ws: Path) -> dict | None:
    entry = ws / "hypr" / "hyprland.lua"
    if not entry.exists():
        return None
    r = subprocess.run(["lua", str(MOCK), str(entry)], capture_output=True, text=True, timeout=20)
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def dig(obj, path: str):
    for part in path.split("."):
        if isinstance(obj, dict) and part in obj:
            obj = obj[part]
        else:
            return KeyError
    return obj


def norm_keys(keys: str) -> str:
    return re.sub(r"\s+", "", str(keys)).upper()


def bind_repr(b: dict) -> str:
    """'window.move [{"workspace": 1}] {"locked": true}' — indépendant de l'ordre des clés."""
    action = b.get("action")
    if isinstance(action, dict) and "dsp" in action:
        text = f"{action['dsp']} {json.dumps(action.get('args', []), sort_keys=True, ensure_ascii=False)}"
    else:
        text = str(action)
    if b.get("opts"):
        text += " " + json.dumps(b["opts"], sort_keys=True, ensure_ascii=False)
    return text


def hues(value) -> list[float]:
    """Teintes (0-360°) de toutes les couleurs trouvées : #rrggbb, rgba(rrggbbaa), rgb(r,g,b), 0xaarrggbb."""
    import colorsys
    text = json.dumps(value) if not isinstance(value, str) else value
    out = []
    for m in re.finditer(r"(?:#|rgba?\(|0x[0-9a-fA-F]{2})([0-9a-fA-F]{6})", text):
        r, g, b = (int(m.group(1)[i:i + 2], 16) / 255 for i in (0, 2, 4))
        h, l, sat = colorsys.rgb_to_hls(r, g, b)
        if sat > 0.25 and 0.1 < l < 0.95:
            out.append(h * 360)
    for m in re.finditer(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", text):
        r, g, b = (int(x) / 255 for x in m.groups())
        h, l, sat = colorsys.rgb_to_hls(r, g, b)
        if sat > 0.25 and 0.1 < l < 0.95:
            out.append(h * 360)
    return out


def compare(value, check: dict) -> tuple[bool, str]:
    """Applique eq / has / lacks / match à une valeur extraite."""
    if value is KeyError or value is None:
        return False, "absent"
    shown = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    shown = shown if len(shown) < 80 else shown[:77] + "…"
    if "eq" in check:
        exp = check["eq"]
        if isinstance(exp, bool):
            return value is exp, shown
        if isinstance(exp, (int, float)):
            is_num = isinstance(value, (int, float)) and not isinstance(value, bool)
            return is_num and abs(value - exp) < 1e-9, shown
        return value == exp, shown
    if "has" in check:
        return isinstance(value, list) and check["has"] in value, shown
    if "lacks" in check:
        return not (isinstance(value, list) and check["lacks"] in value), shown
    if "hue" in check:
        lo, hi = check["hue"]
        found = hues(value)
        return any(lo <= h <= hi for h in found), " ".join(f"{h:.0f}°" for h in found) or shown
    if "match" in check:
        if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
            return any(re.search(check["match"], v) for v in value), shown
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return re.search(check["match"], text) is not None, shown
    return True, shown


# ----------------------------------------------------------------------------- vérifications

@dataclass
class CheckResult:
    label: str
    ok: bool
    got: str = ""


def run_checks(case: dict, ws: Path, changed: list[str], answer: str) -> list[CheckResult]:
    out = []
    wb, hy = None, None
    for check in case["checks"]:
        label, ok, got = "", False, ""
        if "only" in check:
            prefixes = check["only"]
            bad = [f for f in changed if not f.startswith(tuple(prefixes))] if prefixes else changed
            label, ok, got = f"fichiers sous {prefixes or 'aucun'}", not bad, ", ".join(bad)
        elif "file" in check:
            label, ok = f"fichier {check['file']}", (ws / check["file"]).exists()
        elif "waybar" in check or "waybar_custom" in check:
            wb = wb if wb is not None else waybar_conf(ws) or {}
            if "waybar" in check:
                label = f"waybar {check['waybar']}"
                ok, got = compare(dig(wb, check["waybar"]), check)
            else:
                label = f"waybar custom/*.{check['waybar_custom']}"
                values = [v.get(check["waybar_custom"]) for k, v in wb.items()
                          if k.startswith("custom/") and isinstance(v, dict)]
                ok, got = compare([v for v in values if v is not None] or KeyError, check)
        elif "qml" in check or "qml_not" in check or "qml_hue" in check:
            qml = "\n".join(f.read_text(errors="ignore") for f in sorted(ws.glob("quickshell/**/*.qml")))
            if "qml_hue" in check:
                label = f"qml contient une couleur de teinte {check['qml_hue']}"
                ok, got = compare(qml or KeyError, {"hue": check["qml_hue"]})
            else:
                pattern = check.get("qml") or check["qml_not"]
                found = re.search(pattern, qml, re.S) is not None
                ok = found if "qml" in check else not found
                label = f"qml {'~' if 'qml' in check else '!~'} /{pattern[:40]}/"
        elif "css" in check or "css_not" in check:
            css_path = ws / "waybar" / "style.css"
            css = css_path.read_text() if css_path.exists() else ""
            pattern = check.get("css") or check["css_not"]
            found = re.search(pattern, css, re.S) is not None
            ok = found if "css" in check else not found
            label = f"css {'~' if 'css' in check else '!~'} /{pattern[:40]}/"
        elif "conf" in check:
            from .ecosystem import parse
            conf = ws / check["conf"]
            entries = parse(conf, []) if conf.exists() else []
            if "where" in check:  # « timeout=300 » : seulement les blocs qui contiennent cette valeur
                wk, _, wv = check["where"].partition("=")
                blocks = {e.block for e in entries if e.key == wk and e.value == wv}
                entries = [e for e in entries if e.block in blocks]
            values = [e.value for e in entries if f"{e.category}:{e.key}".lstrip(":") == check["key"]]
            label = f"{check['conf'].split('/')[-1]} {check['key']}" + (f" [{check['where']}]" if "where" in check else "")
            got = ", ".join(values)[:80]
            if "has" in check:
                ok = str(check["has"]) in values
            else:
                # « match = '^$' » sur une clé absente = la clé ne doit pas exister
                ok = any(re.search(check["match"], v) for v in values) if values else check["match"] == "^$"
        elif "no_conflicts" in check:
            from .binds import conflicts, describe, from_config
            found = conflicts(from_config(ws / "hypr" / "hyprland.lua"))
            label, ok, got = "aucun conflit de raccourcis", not found, "; ".join(describe(found))[:120]
        elif "bind_any" in check:
            hy = hy if hy is not None else hypr_conf(ws) or {}
            label = f"un raccourci lance /{check['bind_any']}/"
            ok = any(re.search(check["bind_any"], bind_repr(b)) for b in hy.get("binds", []))
        elif any(k in check for k in ("hypr", "bind", "window_rule", "layer_rule", "exec")):
            hy = hy if hy is not None else hypr_conf(ws) or {}
            if "hypr" in check:
                label = f"hypr {check['hypr']}"
                ok, got = compare(dig(hy, check["hypr"]), check)
            elif "bind" in check:
                label = f"bind {check['bind']}"
                binds = [b for b in hy.get("binds", []) if norm_keys(b.get("keys")) == norm_keys(check["bind"])]
                ok, got = compare([bind_repr(b) for b in binds] or KeyError, check)
            elif "window_rule" in check or "layer_rule" in check:
                kind = "window_rule" if "window_rule" in check else "layer_rule"
                needle = check[kind].lower()
                rules = [r for r in hy.get(kind + "s", [])
                         if needle in json.dumps(r.get("match", r), ensure_ascii=False).lower()]
                label = f"{kind} {check[kind]}"
                ok, got = compare(rules or KeyError, check)
            else:
                label = f"exec {check['exec']}"
                ok, got = compare(hy.get("exec", []) or KeyError, {"match": check["exec"]})
        elif "answer" in check:
            label = f"réponse ~ /{check['answer']}/"
            ok = re.search(check["answer"], answer, re.I) is not None
        else:
            label = f"vérification inconnue : {check}"
        for op, sym in (("eq", "="), ("has", "∋"), ("lacks", "∌"), ("match", "~"), ("hue", "teinte")):
            if op in check:
                label += f" {sym} {check[op]!r}" if op != "match" else f" ~ /{check[op][:30]}/"
                break
        out.append(CheckResult(label, bool(ok), got))
    return out


# ----------------------------------------------------------------------------- exécution

@dataclass
class CaseRun:
    id: str
    tags: list[str]
    valid: bool
    first_try: bool
    conform: bool
    passed: int
    total: int
    seconds: float
    model_calls: int
    fixes: int
    checks: list[dict] = field(default_factory=list)
    error: str = ""
    workspace: str = ""


def snapshot(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): p.read_text(errors="ignore")
            for p in root.rglob("*") if p.is_file()}


def run_case(case: dict, model: str, think: bool, out_dir: Path) -> CaseRun:
    ws = Workspace(out_dir / case["id"])
    if ws.root.exists():
        shutil.rmtree(ws.root)
        ws.root.mkdir(parents=True)
    if case.get("seed"):
        shutil.copytree(EVALS / "fixtures" / case["seed"], ws.root, dirs_exist_ok=True)
    before = snapshot(ws.root)

    session = Session(ws, model=model, think=think)
    start, calls, fixes, verify_failed, error = time.time(), 0, 0, False, ""
    try:
        for ev in session.turn(case["request"]):
            if ev.kind == "status" and ev.name == "model":
                calls += 1
            elif ev.kind == "status" and ev.name == "fix":
                fixes += 1
            elif ev.kind == "tool_result" and ev.name.startswith("verify") \
                    and not re.match(r"(config|waybar|quickshell|hypr\w+) ok", ev.text):
                verify_failed = True
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    seconds = time.time() - start

    after = snapshot(ws.root)
    changed = sorted(f for f in after if before.get(f) != after[f])
    result = session.last
    answer = result.summary if result else ""
    checks = run_checks(case, ws.root, changed, answer)
    passed = sum(c.ok for c in checks)
    valid = bool(result and result.ok) and not error
    (ws.root.parent / f"{case['id']}.session.json").write_text(
        json.dumps(session.messages, ensure_ascii=False, indent=1))
    return CaseRun(
        id=case["id"], tags=case.get("tags", []), valid=valid,
        first_try=valid and not verify_failed and fixes == 0,
        conform=passed == len(checks), passed=passed, total=len(checks),
        seconds=round(seconds, 1), model_calls=calls, fixes=fixes,
        checks=[asdict(c) for c in checks], error=error, workspace=str(ws.root),
    )


def load_cases(pattern: str | None, tag: str | None) -> list[dict]:
    cases = tomllib.loads((EVALS / "cases.toml").read_text())["case"]
    if pattern:
        cases = [c for c in cases if re.search(pattern, c["id"])]
    if tag:
        cases = [c for c in cases if tag in c.get("tags", [])]
    return cases


# ----------------------------------------------------------------------------- rapport

def summarize(runs: list[CaseRun]) -> dict:
    n = len(runs) or 1
    return {
        "cases": len(runs),
        "valid": sum(r.valid for r in runs) / n,
        "first_try": sum(r.first_try for r in runs) / n,
        "conform": sum(r.conform for r in runs) / n,
        "checks": sum(r.passed for r in runs) / max(1, sum(r.total for r in runs)),
        "avg_seconds": sum(r.seconds for r in runs) / n,
    }


def previous_result(model: str) -> dict | None:
    """Dernier run complet (sans filtre) du même modèle, pour afficher les écarts."""
    for f in sorted(RESULTS.glob("*.json"), reverse=True):
        data = json.loads(f.read_text())
        if data["model"] == model and not any(data.get("filter", {}).values()):
            return data
    return None


def mark(ok: bool) -> Text:
    return Text("✓", style=C["green"]) if ok else Text("✗", style=C["red"])


def print_report(runs: list[CaseRun], summary: dict, prev: dict | None):
    t = Table(box=None, padding=(0, 2), header_style=C["overlay0"])
    for col, j in (("cas", "left"), ("valide", "center"), ("1er coup", "center"), ("conforme", "center"),
                   ("vérifs", "right"), ("temps", "right"), ("appels", "right"), ("échecs", "left")):
        t.add_column(col, justify=j)
    for r in runs:
        failed = [c["label"] + (f" ({c['got']})" if c["got"] else "") for c in r.checks if not c["ok"]]
        if r.error:
            failed.insert(0, r.error)
        t.add_row(Text(r.id, style=C["text"]), mark(r.valid), mark(r.first_try), mark(r.conform),
                  Text(f"{r.passed}/{r.total}", style=C["subtext"]), Text(f"{r.seconds:.0f}s", style=C["overlay0"]),
                  Text(str(r.model_calls), style=C["overlay0"]),
                  Text("; ".join(failed)[:90], style=C["peach"]))
    console.print(t)

    prev_s = prev["summary"] if prev else None
    console.print()
    for key, label in (("valid", "valide"), ("first_try", "valide du 1er coup"), ("conform", "conforme"),
                       ("checks", "vérifications passées")):
        line = Text.assemble((f"  {label:<24}", C["subtext"]), (f"{summary[key] * 100:5.1f} %", f"bold {C['text']}"))
        if prev_s and key in prev_s:
            delta = (summary[key] - prev_s[key]) * 100
            line.append(f"   {delta:+.1f}", style=C["green"] if delta > 0 else C["red"] if delta < 0 else C["overlay0"])
        console.print(line)
    console.print(Text.assemble(("  temps moyen             ", C["subtext"]),
                                (f"{summary['avg_seconds']:.0f} s", f"bold {C['text']}")))


def main(pattern: str | None = None, tag: str | None = None, model: str = CHAT_MODEL, think: bool = False,
         repeat: int = 1, live: bool = True, label: str = ""):
    if not live:
        os.environ["MYHYPR_NO_LIVE"] = "1"
    os.environ.setdefault("MYHYPR_NO_SYSTEM", "1")  # banc reproductible d'une machine à l'autre
    cases = load_cases(pattern, tag)
    if not cases:
        console.print("[err]Aucun cas ne correspond.[/]")
        return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = EVALS / "runs" / stamp
    console.print(Text.assemble(("◆ ", C["mauve"]), (f"{len(cases)} cas × {repeat}", "bold"),
                                (f"  ·  {model}  ·  think {'on' if think else 'off'}"
                                 f"  ·  waybar live {'on' if live else 'off'}", C["overlay0"])))
    runs: list[CaseRun] = []
    for i in range(repeat):
        for case in cases:
            console.print(Text(f"  … {case['id']}" + (f" #{i + 1}" if repeat > 1 else ""), style=C["overlay0"]),
                          end="\r")
            r = run_case(case, model, think, out_dir / (f"r{i + 1}" if repeat > 1 else ""))
            runs.append(r)
            status = Text.assemble(("  ", ""), mark(r.valid), (" ", ""), mark(r.conform),
                                   (f"  {r.id:<16}", C["text"]), (f"{r.passed}/{r.total}  {r.seconds:.0f}s", C["overlay0"]))
            console.print(status)

    summary = summarize(runs) | {"model": model, "think": think, "repeat": repeat, "label": label}
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{stamp}_{model.replace(':', '-')}{('_' + label) if label else ''}.json"
    prev = previous_result(model)
    out.write_text(json.dumps({"date": stamp, "model": model, "summary": summary,
                               "filter": {"pattern": pattern, "tag": tag},
                               "runs": [asdict(r) for r in runs]}, ensure_ascii=False, indent=1))
    console.print()
    print_report(runs, summary, prev if prev and not pattern and not tag else None)
    console.print(Text(f"\n  résultats : {out.relative_to(ROOT)}   ·   fichiers produits : "
                       f"{out_dir.relative_to(ROOT)}", style=C["overlay0"]))


def rescore(result_file: str):
    """Recalcule les vérifications d'un run existant (après correction du banc), sans relancer le modèle."""
    path = Path(result_file).resolve()
    data = json.loads(path.read_text())
    cases = {c["id"]: c for c in load_cases(None, None)}
    runs = []
    for r in data["runs"]:
        case, ws = cases.get(r["id"]), Path(r["workspace"])
        if case is None or not ws.exists():
            continue
        before = {}
        if case.get("seed"):
            before = snapshot(EVALS / "fixtures" / case["seed"])
        after = snapshot(ws)
        changed = sorted(f for f in after if before.get(f) != after[f])
        session_file = ws.parent / f"{r['id']}.session.json"
        msgs = json.loads(session_file.read_text()) if session_file.exists() else []
        answer = next((m["content"] for m in reversed(msgs) if m["role"] == "assistant" and m["content"]), "")
        checks = run_checks(case, ws, changed, answer)
        passed = sum(c.ok for c in checks)
        runs.append(CaseRun(**{**r, "conform": passed == len(checks), "passed": passed, "total": len(checks),
                               "checks": [asdict(c) for c in checks]}))
    summary = summarize(runs) | {k: data["summary"].get(k) for k in ("model", "think", "repeat")}
    summary["label"] = (data["summary"].get("label") or "") + "+rescored"
    out = path.with_name(path.stem + "_rescored.json")
    out.write_text(json.dumps({**data, "summary": summary, "runs": [asdict(r) for r in runs]},
                              ensure_ascii=False, indent=1))
    print_report(runs, summary, None)
    console.print(Text(f"\n  résultats : {out.relative_to(ROOT)}", style=C["overlay0"]))
