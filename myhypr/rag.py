"""Étape 2 — RAG : indexation de la doc (wiki Hyprland, wiki Waybar, stubs Lua) et recherche."""

import json
import os
import re
from pathlib import Path

import numpy as np
from .llm import client

from .config import DOCS_RAW, EMBED_MODEL, INDEX_DIR

MAX_CHARS = 1800
HYPR_STUBS = Path("/usr/share/hypr/stubs/hl.meta.lua")
HYPR_EXAMPLE = Path("/usr/share/hypr/hyprland.lua")

# Pages du wiki Hyprland sans intérêt pour écrire une config
HYPR_SKIP = ("nix/", "contributing-and-debugging/", "community/", "version-selector")
# Pages Waybar utiles (le reste = vitrines de configs perso)
WAYBAR_KEEP = re.compile(r"^(Module|Configuration|Styling|Examples|FAQ|States|Writing-Modules|Tray-Applets)")


def _split(text: str, max_chars: int = MAX_CHARS) -> list[str]:
    """Découpe un bloc trop long en morceaux, en coupant sur les lignes vides."""
    if len(text) <= max_chars:
        return [text]
    parts, cur = [], ""
    for para in re.split(r"\n\s*\n", text):
        if cur and len(cur) + len(para) > max_chars:
            parts.append(cur)
            cur = ""
        cur = f"{cur}\n\n{para}" if cur else para
        while len(cur) > max_chars:  # paragraphe géant (gros bloc de code)
            parts.append(cur[:max_chars])
            cur = cur[max_chars:]
    if cur.strip():
        parts.append(cur)
    return parts


def _chunk_markdown(path: Path, source: str) -> list[dict]:
    text = path.read_text(errors="ignore")
    text = re.sub(r"^---\n.*?\n---\n", "", text, flags=re.S)  # front-matter Hugo
    chunks, title, buf = [], path.stem, []

    def flush():
        body = "\n".join(buf).strip()
        if len(body) > 40:
            for part in _split(body):
                chunks.append({"source": source, "title": title, "text": part})

    for line in text.splitlines():
        if re.match(r"^#{1,4} ", line):
            flush()
            buf, title = [], f"{path.stem} › {line.lstrip('#').strip()}"
        else:
            buf.append(line)
    flush()
    return chunks


def _chunk_lua_blocks(path: Path, source: str, sep: str) -> list[dict]:
    text = path.read_text(errors="ignore")
    blocks = re.split(sep, text)
    chunks = []
    for block in blocks:
        block = block.strip()
        if len(block) < 40:
            continue
        title = block.splitlines()[0][:80]
        for part in _split(block):
            chunks.append({"source": source, "title": title, "text": part})
    return chunks


def collect_chunks() -> list[dict]:
    chunks: list[dict] = []
    wiki = DOCS_RAW / "hyprland-wiki" / "content"
    for md in sorted(wiki.rglob("*.md")):
        rel = md.relative_to(wiki).as_posix()
        if not rel.startswith(HYPR_SKIP):
            chunks += _chunk_markdown(md, f"hyprland-wiki/{rel}")

    for md in sorted((DOCS_RAW / "waybar-wiki").glob("*.md")):
        if WAYBAR_KEEP.match(md.name):
            chunks += _chunk_markdown(md, f"waybar-wiki/{md.name}")

    qs_docs = DOCS_RAW / "quickshell"
    for md in sorted(qs_docs.rglob("*.md")):
        chunks += _chunk_markdown(md, f"quickshell/{md.relative_to(qs_docs).as_posix()}")

    # Les stubs = référence exacte de l'API Lua installée (une classe par chunk)
    if HYPR_STUBS.exists():
        chunks += _chunk_lua_blocks(HYPR_STUBS, "hl.meta.lua", r"\n(?=---@(?:class|alias) )")
    if HYPR_EXAMPLE.exists():
        chunks += _chunk_lua_blocks(HYPR_EXAMPLE, "example hyprland.lua", r"\n(?=-{4,}\n)")

    # Tes propres exemples : docs/examples/**/* (configs que tu aimes)
    examples = DOCS_RAW.parent / "examples"
    for f in sorted(examples.rglob("*")) if examples.exists() else []:
        if f.is_file() and f.suffix in {".lua", ".jsonc", ".json", ".css", ".qml"}:
            text = f.read_text(errors="ignore")
            for part in _split(text):
                rel = f.relative_to(examples)
                chunks.append({"source": f"examples/{rel}", "title": "/".join(rel.parts[-3:]), "text": part})
    return chunks


def _embed(texts: list[str], prefix: str) -> np.ndarray:
    vecs = []
    for i in range(0, len(texts), 64):
        batch = [prefix + t for t in texts[i:i + 64]]
        vecs += client.embed(model=EMBED_MODEL, input=batch, truncate=True).embeddings
    arr = np.asarray(vecs, dtype=np.float32)
    return arr / np.linalg.norm(arr, axis=1, keepdims=True)


def build_index() -> int:
    chunks = collect_chunks()
    texts = [f"{c['title']}\n{c['text']}" for c in chunks]
    print(f"{len(chunks)} morceaux à indexer…")
    vecs = _embed(texts, "search_document: ")
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    np.save(INDEX_DIR / "vectors.npy", vecs)
    (INDEX_DIR / "chunks.json").write_text(json.dumps(chunks, ensure_ascii=False))
    return len(chunks)


_cache: tuple[np.ndarray, list[dict]] | None = None


def search(query: str, k: int = 5, kind: str | None = None) -> list[dict]:
    """kind = "docs" (doc officielle), "examples" (configs réelles) ou None (tout)."""
    global _cache
    if _cache is None:
        if not (INDEX_DIR / "vectors.npy").exists():
            raise RuntimeError("Index absent : lance `myhypr index` d'abord.")
        _cache = (np.load(INDEX_DIR / "vectors.npy"),
                  json.loads((INDEX_DIR / "chunks.json").read_text()))
    vecs, chunks = _cache
    q = _embed([query], "search_query: ")[0]
    scores = vecs @ q
    hits = []
    for i in np.argsort(-scores):
        is_example = chunks[i]["source"].startswith("examples/")
        if kind == "docs" and is_example or kind == "examples" and not is_example:
            continue
        hits.append({**chunks[i], "score": float(scores[i])})
        if len(hits) == k:
            break
    return hits


def search_mixed(query: str, docs: int = 3, examples: int = 2) -> str:
    if os.environ.get("MYHYPR_NO_EXAMPLES"):  # pour mesurer l'apport des exemples dans le banc
        examples = 0
    out = format_hits(search(query, k=docs, kind="docs"))
    ex = search(query, k=examples, kind="examples") if examples else []
    if ex:
        out += "\n\n## Exemples de configs réelles (validées sur ta version) — inspire-toi du style, " \
               "adapte les valeurs à la demande\n\n" + format_hits(ex)
    return out


def format_hits(hits: list[dict]) -> str:
    return "\n\n".join(f"### [{h['source']}] {h['title']}\n{h['text']}" for h in hits)
