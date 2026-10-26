"""Variantes des demandes du banc, pour diversifier le dataset (elles ne servent PAS de référence de score).

Deux sources, toutes vérifiables par le banc :
- modèles paramétrés (evals/variants/templates.toml) : mêmes vérifications, avec les valeurs substituées ;
- reformulations des 38 demandes du banc par le modèle de base. Filtre strict : une reformulation est rejetée si elle
  perd un chiffre, un chemin, une couleur ou un terme repris par les vérifications → les vérifications d'origine
  restent valables.
Sortie : evals/variants/generated.json, lu par `myhypr eval --variants` et par la récolte des candidats.
"""

import copy
import json
import re
import tomllib
from pathlib import Path


from .config import ROOT
from .llm import client

DIR = ROOT / "evals" / "variants"
TEMPLATES = DIR / "templates.toml"
GENERATED = DIR / "generated.json"
PARAPHRASE_MODEL = "qwen3.5:9b"   # modèle de base : pas de prompt système de l'agent
STYLES = ["en tutoyant, de façon familière et courte", "de façon plus détaillée et polie, en vouvoyant",
          "en anglais", "comme un message rapide tapé au clavier, sans majuscules"]


def _subst(obj, values: dict):
    if isinstance(obj, str):
        whole = re.fullmatch(r"\{(\w+)\}", obj)
        if whole and whole.group(1) in values:
            return values[whole.group(1)]          # garde le type (nombre…)
        return re.sub(r"\{(\w+)\}", lambda m: str(values.get(m.group(1), m.group(0))), obj)
    if isinstance(obj, list):
        return [_subst(x, values) for x in obj]
    if isinstance(obj, dict):
        return {k: _subst(v, values) for k, v in obj.items()}
    return obj


def from_templates() -> list[dict]:
    data = tomllib.loads(TEMPLATES.read_text())
    cases = []
    for t in data.get("template", []):
        for i, values in enumerate(t["values"], 1):
            case = {k: copy.deepcopy(v) for k, v in t.items() if k != "values"}
            case = _subst(case, values)
            case["id"] = f"v-{t['id']}-{i}"
            case["tags"] = [*t.get("tags", []), "variant", "template"]
            case["parent"] = t["id"]
            cases.append(case)
    return cases


MODS = r"(?:SUPER|CTRL|CONTROL|ALT|SHIFT|MOD4|WIN)"


def _norm(text: str) -> str:
    """Minuscules, espaces retirés autour des « + » (SUPER + Q ≡ super+q)."""
    return re.sub(r"\s*\+\s*", "+", text.lower())


def _required_tokens(case: dict) -> set[str]:
    """Ce qu'une reformulation DOIT garder : chiffres, chemins, couleurs, combinaisons de touches, et les termes de la
    demande repris par les vérifications (kitty, pavucontrol, Calculator…)."""
    req = case["request"]
    low = _norm(req)
    tokens = set(re.findall(r"(?<![\w#])\d+(?:[.,]\d+)?", req))          # pas les chiffres d'un #1e1e2e
    tokens |= set(re.findall(r"[\w~./-]+\.(?:lua|conf|css|jsonc|qml|png|toml)\b", req))
    tokens |= {c.lower() for c in re.findall(r"#[0-9a-fA-F]{6}", req)}
    tokens |= {_norm(m) for m in re.findall(rf"{MODS}(?:\s*\+\s*{MODS})*\s*\+\s*[\w]+", req, re.I)}
    check_text = json.dumps(case.get("checks", []), ensure_ascii=False).lower()
    for w in re.findall(r"[a-z][a-z0-9_-]{2,}", check_text):
        if w in low and w not in {"true", "false", "hypr", "waybar", "config", "match", "general", "input", "modules",
                                  "left", "right", "center", "file", "only"}:
            tokens.add(w)
    return tokens


ROLE_SWAP = re.compile(r"\b(que je|je vais|je peux|je te|je vous|voulez-vous que|tu veux que|"
                       r"shall i|do you want me|i will|i can)\b", re.I)


def _keeps(original: dict, text: str) -> bool:
    low = _norm(text)
    paths = re.findall(r"[\w~./-]+\.(?:lua|conf|css|jsonc|qml|png|toml)\b", original["request"])
    return (all(_norm(t) in low for t in _required_tokens(original))
            and all(p in text for p in paths)                       # chemins : casse exacte
            and not ROLE_SWAP.search(text)                          # c'est l'utilisateur qui demande
            and len(text) > 15 and text.strip().lower() != original["request"].strip().lower())


def paraphrases(case: dict, n: int) -> list[str]:
    styles = STYLES[:n]
    prompt = ("Reformule cette demande de configuration Linux de plusieurs façons. Chaque reformulation doit garder "
              "EXACTEMENT les mêmes exigences : mêmes valeurs, mêmes touches, mêmes noms de fichiers, de programmes "
              "et de couleurs, rien en plus, rien en moins.\n\n"
              f"Demande : {case['request']}\n\n"
              "Styles, un par reformulation, dans cet ordre :\n" + "\n".join(f"- {s}" for s in styles) +
              "\n\nRéponds uniquement par un tableau JSON de chaînes.")
    try:
        r = client.chat(model=PARAPHRASE_MODEL, messages=[{"role": "user", "content": prompt}], think=False,
                        options={"temperature": 0.8}, format="json")
        data = json.loads(r.message.content)
    except Exception:
        return []
    # selon les appels : [..], {"reformulations": [..]} ou {"reformulation_1": "..", …} → toutes les chaînes
    def strings(x):
        if isinstance(x, str):
            yield x
        elif isinstance(x, list):
            for y in x:
                yield from strings(y)
        elif isinstance(x, dict):
            for y in x.values():
                yield from strings(y)
    return [t.strip() for t in strings(data) if _keeps(case, t)][:n]


def generate(n_paraphrases: int = 2, progress=print) -> list[dict]:
    from .evaluate import load_cases
    cases = from_templates()
    progress(f"{len(cases)} variantes issues des modèles paramétrés")
    kept = rejected = 0
    for case in load_cases(None, None, source="bench"):
        texts = paraphrases(case, n_paraphrases)
        rejected += n_paraphrases - len(texts)
        for i, text in enumerate(texts, 1):
            v = copy.deepcopy(case)
            v.update(id=f"v-{case['id']}-p{i}", request=text, parent=case["id"],
                     tags=[*case.get("tags", []), "variant", "paraphrase"])
            cases.append(v)
            kept += 1
        progress(f"  {case['id']:<20} {len(texts)}/{n_paraphrases} reformulation(s) gardée(s)")
    DIR.mkdir(parents=True, exist_ok=True)
    GENERATED.write_text(json.dumps(cases, ensure_ascii=False, indent=1))
    progress(f"{len(cases)} variantes au total ({kept} reformulations gardées, {rejected} rejetées par le filtre) "
             f"→ {GENERATED.relative_to(ROOT)}")
    return cases


def load() -> list[dict]:
    return json.loads(GENERATED.read_text()) if GENERATED.exists() else []
