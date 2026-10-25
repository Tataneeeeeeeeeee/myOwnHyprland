"""Interface conversationnelle : `uv run myhypr`."""

import asyncio
import difflib
import json
import re
import shlex
import shutil
import sys
import termios
import time
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.history import FileHistory
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from . import binds, dataset, ops, rag
from .agent import Session
from .system import detect
from .config import CHAT_MODEL, DATA, INDEX_DIR, SESSIONS_DIR, WORKSPACES
from .theme import C, CODE_THEME, LOGO, PT_STYLE, RICH_THEME, gradient
from .tools import Workspace

console = Console(theme=RICH_THEME, highlight=False)
HOME = str(Path.home())
KEEP_SECONDS = 20

COMMANDS = {
    "Conversation": {
        "/new": "nouvelle conversation et nouveau dossier de travail",
        "/from": "partir de ta config actuelle  ·  /from hypr waybar",
        "/think": "raisonnement du modèle on/off (plus lent)",
        "/model": "afficher ou changer de modèle",
        "/system": "ce que l'agent sait de ta machine (écrans, clavier, GPU, logiciels)",
        "/binds": "conflits de raccourcis (session en cours + dossier de travail)",
    },
    "Fichiers": {
        "/files": "fichiers produits",
        "/show": "afficher un fichier  ·  /show waybar/style.css",
        "/diff": "différences avec ~/.config",
    },
    "Appliquer": {
        "/apply": "copier dans ~/.config (diff + confirmation + sauvegarde)",
        "/restore": "lister ou restaurer une sauvegarde",
    },
    "Entraînement": {
        "/keep": "garder le dernier résultat pour le fine-tuning",
        "/dataset": "avancement du dataset",
        "/search": "tester la recherche dans la doc",
    },
    "": {
        "/clear": "effacer l'écran",
        "/help": "cette aide",
        "/quit": "quitter  ·  Ctrl+D",
    },
}
ALL_COMMANDS = {c: d for group in COMMANDS.values() for c, d in group.items()}

LOG_PREFIX = re.compile(r"^\[\d{4}-\d\d-\d\d [^\]]+\]\s*(\[\w+\]\s*)?")


def short(path) -> str:
    return str(path).replace(HOME, "~", 1)


def elapsed(seconds: float) -> str:
    return f"{seconds:.0f}s" if seconds < 60 else f"{int(seconds // 60)}m{int(seconds % 60):02d}s"


# --------------------------------------------------------------------------- complétion

class SlashCompleter(Completer):
    def __init__(self, app: "App"):
        self.app = app

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        if " " not in text:
            for cmd, desc in ALL_COMMANDS.items():
                if cmd.startswith(text):
                    yield Completion(cmd, -len(text), display_meta=desc)
            return
        cmd, _, arg = text.partition(" ")
        word = arg.rsplit(" ", 1)[-1]
        choices = {"/show": self.app.ws.files, "/diff": self.app.ws.files, "/restore": ops.backups,
                   "/from": lambda: ["hypr", "waybar", "quickshell", "rofi", "kitty", "swaync", "wlogout"]}
        for c in choices.get(cmd, list)():
            if c.startswith(word):
                yield Completion(c, -len(word))


# --------------------------------------------------------------------------- rendu d'un tour

def row(icon: str, icon_style: str, body, meta: Text | str = "") -> Table:
    """Une ligne d'activité alignée : icône · contenu · méta à droite."""
    t = Table.grid(padding=(0, 1))
    t.add_column(width=2, no_wrap=True)
    t.add_column(ratio=1)
    t.add_column(justify="right", no_wrap=True)
    t.add_row(Text(icon, style=icon_style), body, meta)
    return Padding(t, (0, 0, 0, 1))


class Ticker:
    """Spinner + phase + chrono, recalculé à chaque rafraîchissement du Live."""
    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, renderer: "Renderer"):
        self.r = renderer

    def __rich__(self):
        r = self.r
        frame = self.FRAMES[int(time.monotonic() * 12) % len(self.FRAMES)]
        line = Text()
        line.append(f" {frame} ", style=f"bold {C['mauve']}")
        line.append(r.phase, style=C["subtext"])
        line.append(f"  {elapsed(time.time() - r.started)}", style=C["overlay0"])
        line.append("  ·  ctrl+c pour interrompre", style=C["surface2"])
        if not r.buf.strip():
            return line
        # Texte en cours de génération : seulement la fin, pour ne jamais dépasser l'écran
        height = max(4, console.size.height - 6)
        lines = r.buf.strip().splitlines()[-height:]
        preview = Text("\n".join(lines), style=f"italic {C['overlay1']}")
        return Group(Padding(preview, (0, 0, 0, 4)), Text(), line)


class Renderer:
    """Transforme les événements de l'agent en affichage."""

    def __init__(self, ws: Workspace):
        self.ws = ws
        self.started = time.time()
        self.phase = "Lecture de la doc…"
        self.buf = ""
        self.before: str | None = None
        self.live = Live(Ticker(self), console=console, refresh_per_second=12, transient=True)
        self.last_kind: str | None = None

    def __enter__(self):
        self.live.start()
        return self

    def __exit__(self, *exc):
        self.live.stop()

    def print(self, *items, kind: str = "tool"):
        """Ligne vide entre deux groupes de nature différente (narration, outils, statut, réponse) : plus aéré."""
        if self.last_kind is not None and kind != self.last_kind:
            self.live.console.print()
        self.live.console.print(*items)
        self.last_kind = kind

    def flush(self, final: bool):
        text, self.buf = self.buf.strip(), ""
        if not text:
            return
        if final:
            body = Table.grid(padding=(0, 1))
            body.add_column(width=2)
            body.add_column(ratio=1)
            body.add_row(Text("◆", style=f"bold {C['mauve']}"), Markdown(text, code_theme=CODE_THEME))
            self.print(Padding(body, (0, 0, 0, 1)), kind="answer")
        else:
            # Narration intermédiaire : repliée et discrète
            plain = re.sub(r"\*\*|__|`|^#+\s*|^\s*[-*]\s+", "", text, flags=re.M)
            lines = [ln for ln in plain.splitlines() if ln.strip()]
            shown = "\n".join(lines[:2]) + (f"  (+{len(lines) - 2} lignes)" if len(lines) > 2 else "")
            self.print(Padding(Text(shown, style=f"italic {C['overlay1']}"), (0, 0, 0, 4)), kind="narration")

    def on_text(self, delta: str):
        self.buf += delta
        self.phase = "Rédaction…"

    def on_thinking(self):
        self.phase = "Réflexion…"

    # ---- outils
    def on_tool_call(self, name: str, args: dict):
        self.flush(final=False)
        self.before = None
        if name in ("write_file", "edit_file"):
            p = self.ws.path(args.get("path", ""))
            self.before = p.read_text(errors="ignore") if p.exists() else ""
        self.phase = {
            "write_file": f"Écriture de {args.get('path', '')}…",
            "edit_file": f"Modification de {args.get('path', '')}…",
            "read_file": f"Lecture de {args.get('path', '')}…",
            "search_docs": "Recherche dans la doc…",
            "verify_hyprland": "Validation Hyprland…",
            "verify_waybar": "Validation Waybar (test live 3 s)…",
            "verify_quickshell": "Validation Quickshell (qmllint + test live 4 s)…",
            "system_info": "Détection du système…",
            "check_binds": "Analyse des raccourcis…",
        }.get(name, f"{name}…")
        self.args = args

    def on_tool_result(self, name: str, out: str):
        args = getattr(self, "args", {})
        path = args.get("path", "")
        if name in ("write_file", "edit_file") and not out.startswith("ERREUR"):
            p = self.ws.path(path)
            new = p.read_text(errors="ignore") if p.exists() else args.get("content", "")
            meta = Text()
            if self.before:
                sm = difflib.SequenceMatcher(None, self.before.splitlines(), new.splitlines())
                add = sum(j2 - j1 for op, _, _, j1, j2 in sm.get_opcodes() if op in ("insert", "replace"))
                rem = sum(i2 - i1 for op, i1, i2, _, _ in sm.get_opcodes() if op in ("delete", "replace"))
                meta.append(f"+{add}", style=C["green"]).append(" ").append(f"−{rem}", style=C["red"])
            else:
                meta.append(f"nouveau · {len(new.splitlines())} lignes", style=C["green"])
            self.print(row("✎", C["blue"], Text(path, style=C["text"]), meta))
        elif name == "edit_file":
            self.print(row("✎", C["peach"], Text(path, style=C["text"]), Text(out[:60], style=C["peach"])))
        elif name == "read_file":
            self.print(row("◌", C["overlay0"], Text(f"lu {path}", style=C["overlay1"]),
                           Text(f"{len(out.splitlines())} lignes", style=C["overlay0"])))
        elif name == "list_files":
            self.print(row("◌", C["overlay0"], Text("liste des fichiers", style=C["overlay1"])))
        elif name == "search_docs":
            titles = [ln[4:].split("] ", 1)[-1] for ln in out.splitlines() if ln.startswith("### ")]
            pages = list(dict.fromkeys(t.split(" › ")[0].replace("Module:-", "").replace("-", " ") for t in titles))
            body = Text.assemble(("doc ", C["teal"]), (f"« {args.get('query', '')} »", C["subtext"]))
            self.print(row("⌕", C["teal"], body, Text(" · ".join(pages)[:48], style=C["overlay0"])))
        elif name.startswith("verify"):
            what = {"verify_hyprland": "Hyprland", "verify_waybar": "Waybar"}.get(name, "Quickshell")
            lines = [LOG_PREFIX.sub("", ln) for ln in out.splitlines() if ln.strip()]
            if lines and lines[0].startswith(("config ok", "waybar ok", "quickshell ok")):
                self.print(row("✓", f"bold {C['green']}", Text(f"{what} valide", style=C["green"])))
            else:
                errors = [ln for ln in lines if not ln.startswith("Indice")]
                hints = [ln.removeprefix("Indice : ") for ln in lines if ln.startswith("Indice")]
                n = len(errors)
                self.print(row("✗", f"bold {C['red']}", Text(f"{what} : {n} erreur{'s' * (n > 1)}", style=C["red"])))
                for ln in errors[:4]:
                    self.print(Padding(Text(f"│ {ln}", style=C["red"]), (0, 0, 0, 4)))
                for ln in hints:
                    self.print(Padding(Text(f"↳ {ln}", style=C["peach"]), (0, 0, 0, 4)))
        else:
            self.print(row("·", C["overlay0"], Text(f"{name} : {out.splitlines()[0] if out else ''}")))
        self.phase = "Génération…"

    def on_status(self, name: str, args: dict):
        if name == "model":
            if self.phase.startswith(("Lecture", "Génération")):
                self.phase = "Génération…"
            return
        self.flush(final=False)
        if name == "review":
            self.print(row("◇", C["lavender"], Text("relecture de la demande point par point", style=C["lavender"])),
                       kind="status")
            self.phase = "Relecture…"
        elif name == "fix":
            self.print(row("↻", C["peach"], Text(f"correction {args.get('round')}/{args.get('max')}",
                                                  style=C["peach"])), kind="status")
            self.phase = "Correction…"
        elif name == "conflict":
            self.print(row("!", C["peach"], Text("conflit de raccourcis créé → correction", style=C["peach"])),
                       kind="status")
            for line in args.get("lines", [])[:4]:
                self.print(Padding(Text(f"│ {line}", style=C["peach"]), (0, 0, 0, 4)), kind="status")
            self.phase = "Correction…"
        elif name == "compact":
            self.print(row("◇", C["overlay1"], Text("contexte presque plein : anciens résultats d'outils retirés",
                                                    style=C["overlay1"])), kind="status")
        elif name == "truncated":
            self.print(row("!", C["peach"], Text("réponse coupée : limite de contexte atteinte — /new pour repartir "
                                                 "d'une conversation vide", style=C["peach"])), kind="status")


# --------------------------------------------------------------------------- application

class App:
    def __init__(self, model: str = CHAT_MODEL):
        self.model = model
        self.think = False
        self.new_workspace()
        DATA.mkdir(parents=True, exist_ok=True)
        self.prompt = PromptSession(
            history=FileHistory(str(DATA / ".history")),
            completer=SlashCompleter(self), complete_while_typing=True, style=PT_STYLE,
            placeholder=[("class:placeholder", "Décris la config que tu veux…   / pour les commandes")],
            reserve_space_for_menu=8,
        )

    # ---- état
    def new_workspace(self):
        old = getattr(self, "ws", None)
        if old and not old.files():
            old.root.rmdir()
        self.ws = Workspace(WORKSPACES / time.strftime("%Y%m%d-%H%M%S"))
        self.session = Session(self.ws, model=self.model, think=self.think)

    def width(self) -> int:
        return shutil.get_terminal_size().columns

    def status_parts(self) -> list[tuple[str, str]]:
        parts = [("class:tb.model", self.model), ("class:rule", " · "),
                 ("class:tb.on" if self.think else "class:tb.off", "think " + ("on" if self.think else "off"))]
        n = len(self.ws.files())
        parts += [("class:rule", " · "), ("class:tb.off", f"{n} fichier{'s' * (n > 1)}")]
        last = self.session.last
        if last is not None and n:
            parts += [("class:rule", " · "), ("class:tb.ok", "✓ valide") if last.ok else ("class:tb.err", "✗ erreurs")]
        if self.session.tokens:
            parts += [("class:rule", " · "), ("class:tb.off", f"{self.session.tokens / 1000:.1f}k tok")]
        return parts

    def prompt_message(self):
        """Ligne de séparation avec l'état intégré à droite, puis le curseur."""
        status = self.status_parts()
        used = sum(len(t) for _, t in status) + 4
        return [("class:rule", "─" * max(4, self.width() - used) + " "), *status,
                ("class:rule", " ─\n"), ("class:prompt", "❯ ")]

    def banner(self):
        try:
            n_docs = len(json.loads((INDEX_DIR / "chunks.json").read_text()))
        except (OSError, ValueError):
            n_docs = 0
        n_data = sum(1 for _ in dataset.DATASET.open()) if dataset.DATASET.exists() else 0

        logo = Text("\n").join(gradient(line) for line in LOGO)
        title = Text.assemble(("myOwnHyprland\n", f"bold {C['text']}"),
                              ("agent local de config Hyprland & Waybar", C["overlay1"]))
        head = Table.grid(padding=(0, 3))
        head.add_row(logo, title)

        info = Table.grid(padding=(0, 2))
        info.add_column(style=C["overlay0"])
        info.add_column(style=C["subtext"])
        info.add_row("modèle", self.model)
        info.add_row("travail", short(self.ws.root))
        info.add_row("doc", f"{n_docs} passages indexés" if n_docs else Text("index absent · myhypr index",
                                                                               style=C["peach"]))
        info.add_row("dataset", f"{n_data} / 300 exemples")
        try:
            sysinfo = detect()
            screens = ", ".join(f"{m['nom']} {m['mode'].split('@')[0]}@{float(m['mode'].split('@')[1]):.0f}"
                                for m in sysinfo["ecrans"])
            gpu = "+".join(c["vendor"] for c in sysinfo["gpu"]["cartes"])
            info.add_row("machine", f"Hyprland {sysinfo['hyprland']['version']} · {screens} · "
                                    f"clavier {sysinfo['entrees']['disposition']} · {gpu}")
        except Exception:
            pass

        console.print(Panel(Group(head, Text(), info), border_style=C["surface1"], padding=(1, 2),
                            width=min(self.width(), 78)))
        tips = Text.assemble(("  essaie  ", C["overlay0"]),
                             ("« une waybar flottante avec des workspaces en pastilles »", f"italic {C['subtext']}"),
                             ("\n  ou      ", C["overlay0"]), ("/from hypr waybar", C["mauve"]),
                             (" pour retoucher ta config actuelle", C["overlay0"]))
        console.print(tips, "")

    def echo_user(self, line: str):
        """Remplace la zone de saisie par un bloc message propre."""
        prompt_rows = 1 + max(1, -(-(len(line) + 2) // self.width()))
        sys.stdout.write(f"\x1b[{prompt_rows}F\x1b[J")
        sys.stdout.flush()
        msg = Text(f" ❯ {line} ", style=f"{C['text']} on {C['surface0']}")
        console.print(msg)

    def save(self):
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        (SESSIONS_DIR / f"{self.ws.root.name}.json").write_text(json.dumps(
            {"workspace": str(self.ws.root), "model": self.model, "messages": self.session.messages},
            ensure_ascii=False, indent=2))

    # ---- un tour de conversation
    def ask(self, request: str):
        # Ce que l'utilisateur tape pendant que l'agent travaille ne doit pas casser l'affichage
        tty = termios.tcgetattr(sys.stdin) if sys.stdin.isatty() else None
        if tty:
            quiet = termios.tcgetattr(sys.stdin)
            quiet[3] &= ~termios.ECHO
            termios.tcsetattr(sys.stdin, termios.TCSANOW, quiet)
        try:
            self._ask(request)
        finally:
            if tty:
                termios.tcsetattr(sys.stdin, termios.TCSANOW, tty)
                termios.tcflush(sys.stdin, termios.TCIFLUSH)

    def _ask(self, request: str):
        r = Renderer(self.ws)
        result = None
        try:
            with r:
                for ev in self.session.turn(request):
                    match ev.kind:
                        case "thinking":
                            r.on_thinking()
                        case "text":
                            r.on_text(ev.text)
                        case "tool_call":
                            r.on_tool_call(ev.name, ev.args)
                        case "tool_result":
                            r.on_tool_result(ev.name, ev.text)
                        case "status":
                            r.on_status(ev.name, ev.args)
                        case "done":
                            r.flush(final=True)
                            result = ev.result
        except KeyboardInterrupt:
            self.session.cancel_pending()
            console.print(row("■", C["peach"], Text("interrompu — précise ta demande", style=C["peach"])))
        except Exception as e:
            self.session.cancel_pending()
            console.print(row("✗", C["red"], Text(f"erreur : {e}", style=C["red"])))
        finally:
            self.save()
        if result is not None:
            self.footer(result, time.time() - r.started)
        console.print()

    def footer(self, result, secs: float):
        changed = getattr(result, "changed", [])
        if result.ok and not changed:
            return  # question, description… : rien n'a été modifié, rien à valider
        console.print()
        if result.ok:
            n = len(changed)
            head = Text.assemble((" ✓ ", f"bold {C['base']} on {C['green']}"),
                                 ("  config valide", f"bold {C['green']}"),
                                 (f"  ·  {n} fichier{'s' * (n > 1)} modifié{'s' * (n > 1)}  ·  {elapsed(secs)}",
                                  C["overlay0"]))
            console.print(head)
            hint = Text("   ")
            for cmd, what in (("/diff", "voir"), ("/apply", "appliquer"), ("/keep", "garder pour le dataset")):
                hint.append(cmd, style=C["mauve"]).append(f" {what}    ", style=C["overlay0"])
            console.print(hint)
        else:
            console.print(Text.assemble((" ✗ ", f"bold {C['base']} on {C['red']}"),
                                        ("  il reste des erreurs", f"bold {C['red']}"),
                                        (f"  ·  {elapsed(secs)}", C["overlay0"])))
            for err in result.errors:
                for ln in err.splitlines()[:4]:
                    console.print(Padding(Text(LOG_PREFIX.sub("", ln), style=C["red"]), (0, 0, 0, 4)))
            console.print(Text("   dis-moi comment corriger, ou reformule la demande", style=C["overlay0"]))

    # ---- commandes
    def confirm(self, question: str) -> bool:
        try:
            ans = PromptSession(style=PT_STYLE).prompt(
                [("class:prompt", "? "), ("", question + " "), ("class:placeholder", "[o/N] ")])
            return ans.strip().lower() in ("o", "oui", "y", "yes")
        except (KeyboardInterrupt, EOFError):
            return False

    def note(self, text: str, style: str = C["overlay1"], icon: str = "·"):
        console.print(row(icon, style, Text(text, style=style)))

    def command(self, line: str) -> bool:
        """Renvoie False pour quitter."""
        try:
            cmd, *args = shlex.split(line)
        except ValueError:
            cmd, *args = line.split()
        match cmd:
            case "/quit" | "/exit":
                return False
            case "/help":
                self.help()
            case "/new":
                self.new_workspace()
                self.note(f"nouvelle conversation · {short(self.ws.root)}", C["mauve"], "◆")
            case "/from":
                names = ops.seed(self.ws, args or ["hypr", "waybar"])
                self.note(f"copié dans le dossier de travail : {', '.join(names) or 'rien'}", C["blue"], "✎")
            case "/files":
                self.files()
            case "/show":
                self.show(args[0] if args else None)
            case "/diff":
                d = ops.diff(self.ws, args[0] if args else None)
                if d:
                    console.print(Panel(Syntax(d, "diff", theme=CODE_THEME, word_wrap=True, background_color="default"),
                                        title=Text(" diff avec ~/.config ", style=C["subtext"]), title_align="left",
                                        border_style=C["surface1"]))
                else:
                    self.note("aucune différence avec ~/.config")
            case "/apply":
                self.apply()
            case "/restore":
                if not args:
                    backups = ops.backups()
                    self.note("sauvegardes : " + (", ".join(backups[-8:]) if backups else "aucune"))
                elif self.confirm(f"Restaurer {args[0]} dans ~/.config ?"):
                    try:
                        self.note(f"restauré : {', '.join(ops.restore(args[0]))}", C["green"], "✓")
                    except FileNotFoundError as e:
                        self.note(str(e), C["red"], "✗")
            case "/keep":
                last = self.session.last
                if not last or not last.ok or not dataset.written_files(last):
                    self.note("rien à garder : il faut un résultat valide qui a écrit des fichiers")
                else:
                    dataset.add(last)
                    self.note(f"ajouté au dataset — {dataset.stats()}", C["green"], "✓")
            case "/dataset":
                self.note(dataset.stats())
            case "/search":
                t = Table.grid(padding=(0, 2))
                for h in rag.search(" ".join(args), k=6):
                    t.add_row(Text(f"{h['score']:.2f}", style=C["mauve"]), Text(h["source"], style=C["subtext"]),
                              Text(h["title"].split(" › ")[-1], style=C["overlay0"]))
                console.print(Padding(t, (0, 0, 0, 2)))
            case "/think":
                self.think = self.session.think = not self.think
                self.note(f"raisonnement {'activé — plus lent, parfois meilleur' if self.think else 'désactivé'}",
                          C["mauve"], "◆")
            case "/binds":
                self.binds_report()
            case "/system":
                self.system()
            case "/model":
                if args:
                    self.model = self.session.model = args[0]
                self.note(f"modèle : {self.model}", C["mauve"], "◆")
            case "/clear":
                console.clear()
            case _:
                self.note(f"commande inconnue : {cmd}  (/help)", C["red"], "✗")
        return True

    def binds_report(self):
        sources = [("session en cours", binds.from_session())]
        entry = self.ws.root / "hypr" / "hyprland.lua"
        if entry.exists():
            sources.append(("dossier de travail", binds.from_config(entry)))
        for label, found in sources:
            conf, dup = binds.conflicts(found), binds.duplicates(found)
            color = C["red"] if conf else C["green"]
            self.note(f"{label} : {len(found)} raccourcis · {len(conf)} conflit(s) · {len(dup)} doublon(s)",
                      color, "✗" if conf else "✓")
            for line in binds.describe(conf):
                console.print(Padding(Text(f"│ {line}", style=C["red"]), (0, 0, 0, 4)))
            for line in binds.describe(dup):
                console.print(Padding(Text(f"│ {line} (même action, inoffensif)", style=C["overlay1"]), (0, 0, 0, 4)))

    def system(self):
        info = detect()
        t = Table.grid(padding=(0, 2))
        t.add_column(style=C["overlay0"], width=14)
        t.add_column(style=C["subtext"])
        h = info["hyprland"]
        t.add_row("Hyprland", f"{h['version']} · config {h['format_config']}")
        for m in info["ecrans"]:
            t.add_row("écran", f"{m['nom']} · {m['mode']} Hz · échelle {m['echelle']} · position {m['position']}"
                               f" · VRR {'oui' if m['vrr'] else 'non'}")
        e = info["entrees"]
        t.add_row("clavier", f"{e['disposition']}{(' / ' + e['variante']) if e['variante'] else ''} ({e['clavier_principal']})")
        t.add_row("souris", ", ".join(e["souris"]) or "—")
        t.add_row("touchpad", ", ".join(e["touchpad"]) or "—")
        g = info["gpu"]
        for c in g["cartes"]:
            t.add_row("GPU", c["name"])
        if g["pilote_nvidia"]:
            t.add_row("", Text(f"pilote NVIDIA {g['pilote_nvidia']} : variables d'environnement NVIDIA nécessaires",
                               style=C["peach"]))
        for cat, bins in info["logiciels"].items():
            t.add_row(cat if len(cat) <= 14 else cat[:13] + "…", ", ".join(bins))
        console.print(Padding(t, (0, 0, 0, 2)))

    def help(self):
        t = Table.grid(padding=(0, 2))
        t.add_column(style=C["overlay0"], width=13)
        t.add_column(style=C["mauve"], width=10)
        t.add_column(style=C["subtext"])
        for group, cmds in COMMANDS.items():
            for i, (c, d) in enumerate(cmds.items()):
                t.add_row(group if i == 0 else "", c, d)
            t.add_row("", "", "")
        t.add_row("Raccourcis", "tab", "compléter les commandes et les fichiers")
        t.add_row("", "ctrl+c", "interrompre l'agent")
        t.add_row("", "ctrl+d", "quitter")
        console.print(Padding(t, (0, 0, 0, 2)))

    def files(self):
        files = self.ws.files()
        if not files:
            self.note("aucun fichier pour l'instant")
            return
        console.print(Text(f"  {short(self.ws.root)}", style=C["overlay0"]))
        for i, f in enumerate(files):
            branch = "└─" if i == len(files) - 1 else "├─"
            size = (self.ws.root / f).stat().st_size
            console.print(Text.assemble((f"  {branch} ", C["surface2"]), (f, C["blue"]),
                                        (f"  {size} o", C["overlay0"])))

    def show(self, rel: str | None):
        if not rel:
            self.note("usage : /show <fichier>   (tab pour compléter)")
            return
        p = self.ws.path(rel)
        if not p.exists():
            self.note(f"{rel} n'existe pas", C["red"], "✗")
            return
        lexer = {".jsonc": "json", ".lua": "lua", ".css": "css"}.get(p.suffix) or Syntax.guess_lexer(str(p))
        console.print(Panel(Syntax(p.read_text(), lexer, theme=CODE_THEME, line_numbers=True, word_wrap=True,
                                   background_color="default"),
                            title=Text(f" {rel} ", style=C["blue"]), title_align="left",
                            border_style=C["surface1"]))

    def apply(self):
        files = self.ws.files()
        if not files:
            self.note("rien à appliquer")
            return
        errors = self.ws.validate_all()
        if errors:
            self.note("la config ne passe pas la validation :", C["red"], "✗")
            for e in errors:
                console.print(Padding(Text(e, style=C["red"]), (0, 0, 0, 4)))
            if not self.confirm("Appliquer quand même ?"):
                return
        self.command("/diff")
        if not self.confirm(f"Appliquer {len(files)} fichier{'s' * (len(files) > 1)} et recharger à chaud ?"):
            return
        styles = {"ok": ("✓", C["green"]), "info": ("·", C["overlay1"]), "warn": ("!", C["peach"]),
                  "err": ("✗", C["red"])}
        report = ops.apply_live(self.ws, keep=lambda: self.keep_changes(KEEP_SECONDS))
        for level, msg in report.steps:
            icon, color = styles[level]
            self.note(msg, color, icon)
        if not report.rolled_back:
            self.note(f"pour revenir en arrière plus tard : /restore {report.backup.name}")

    def keep_changes(self, seconds: int) -> bool:
        """Comme les réglages d'écran : sans « o » dans le délai, on restaure (config qui casse le clavier…)."""
        self.note(f"regarde le résultat : garde les changements avec « o » — restauration automatique dans {seconds} s",
                  C["yellow"], "?")

        async def ask() -> str:
            return await PromptSession(style=PT_STYLE).prompt_async(
                [("class:prompt", "? "), ("", "Garder ? "), ("class:placeholder", "[o/N] ")])
        try:
            answer = asyncio.run(asyncio.wait_for(ask(), timeout=seconds))
        except (asyncio.TimeoutError, KeyboardInterrupt, EOFError):
            console.print()
            return False
        return answer.strip().lower() in ("o", "oui", "y", "yes")

    # ---- boucle
    def run(self):
        console.clear()
        self.banner()
        while True:
            try:
                line = self.prompt.prompt(self.prompt_message).strip()
            except KeyboardInterrupt:
                continue
            except EOFError:
                break
            if not line:
                continue
            self.echo_user(line)
            if line.startswith("/"):
                if not self.command(line):
                    break
                console.print()
            else:
                console.print()
                self.ask(line)
        if not self.ws.files():
            self.ws.root.rmdir()
        console.print(Text("  à plus ✦", style=C["overlay0"]))


def main(model: str = CHAT_MODEL):
    App(model).run()
