"""Récupère la doc Quickshell (guide + référence des types) de la version INSTALLÉE → docs/raw/quickshell/.

Le site redirige par défaut vers une vieille version : on lit `qs --version` et on vise /docs/v<version>/.
Usage : uv run python scripts/fetch_quickshell_docs.py
"""

import html
import re
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "raw" / "quickshell"
SITE = "https://quickshell.org"


def installed_version() -> str:
    out = subprocess.run(["qs", "--version"], capture_output=True, text=True).stdout
    m = re.search(r"Quickshell (\d+\.\d+\.\d+)", out)
    if not m:
        sys.exit("Quickshell introuvable (qs --version)")
    return m.group(1)


def get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "myOwnHyprland-docs-fetcher/0.1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="ignore")


def to_text(page: str) -> str:
    """Extrait le contenu principal d'une page de doc et le convertit en texte façon Markdown."""
    start = page.find('class="docs-content')
    if start < 0:
        return ""
    start = page.rfind("<", 0, start)
    ends = [i for i in (page.find('id="toc"', start), page.find("</main>", start), page.find("<footer", start)) if i > 0]
    body = page[start:min(ends)] if ends else page[start:]
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", body, flags=re.S)
    body = re.sub(r"<button[^>]*>.*?</button>", "", body, flags=re.S)       # boutons « copier »
    body = re.sub(r"<h[1-6][^>]*>", "\n## ", body)
    body = re.sub(r"<pre[^>]*>", "\n```qml\n", body).replace("</pre>", "\n```\n")
    body = re.sub(r"<(br|/p|/div|/section|/li|/h[1-6])[^>]*>", "\n", body)
    body = re.sub(r"<li[^>]*>", "\n- ", body)
    text = html.unescape(re.sub(r"<[^>]+>", "", body))
    text = re.sub(r"\{ this\.classList.*?\">", "", text, flags=re.S)        # reste du JS de copie
    text = re.sub(r"\n\s*Go to\s*\n", " → ", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def main():
    version = installed_version()
    base = f"{SITE}/docs/v{version}"
    index = get(f"{base}/types/")
    links = sorted(set(re.findall(rf'href="(/docs/v{re.escape(version)}/(?:types|guide)/[^"#]+)"', index)))
    links += sorted(set(re.findall(rf'href="(/docs/v{re.escape(version)}/guide/[^"#]+)"', get(f"{base}/guide/"))))
    links = sorted(set(links))
    print(f"Quickshell {version} : {len(links)} pages")

    def fetch(link: str) -> int:
        rel = link.split(f"/v{version}/", 1)[1].strip("/")
        text = to_text(get(SITE + link))
        if len(text) < 80:
            return 0
        dst = OUT / f"{rel}.md"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(f"# {rel.replace('types/', '')}\n\n{text}\n")
        return 1

    if OUT.exists():
        import shutil
        shutil.rmtree(OUT)
    with ThreadPoolExecutor(8) as pool:
        saved = sum(pool.map(fetch, links))
    (OUT / "VERSION").write_text(version + "\n")
    print(f"{saved} pages enregistrées dans {OUT}")


if __name__ == "__main__":
    main()
