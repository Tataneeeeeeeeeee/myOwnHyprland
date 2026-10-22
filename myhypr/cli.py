"""Point d'entrée : myhypr index | ask | apply | dataset."""

import argparse
import sys
import time
from pathlib import Path

from . import agent, dataset, ops, rag
from .config import CHAT_MODEL, CONFIG_HOME, WORKSPACES
from .tools import Workspace


def cmd_index(_args):
    n = rag.build_index()
    print(f"Index construit : {n} morceaux.")


def cmd_search(args):
    print(rag.format_hits(rag.search(args.query, k=args.k)))


def cmd_ask(args):
    ws = Workspace(WORKSPACES / time.strftime("%Y%m%d-%H%M%S"))
    for name in ops.seed(ws, args.from_current or []):
        print(f"Copie de travail de ~/.config/{name}")
    print(f"Dossier de travail : {ws.root}\n")

    result = agent.run(args.request, ws, think=args.think, model=args.model)
    log = agent.save_session(result)

    print(f"\n{'✓ VALIDE' if result.ok else '✗ ÉCHEC'} en {result.duration:.0f}s — fichiers :")
    for f in ws.files():
        print(f"  {ws.root / f}")
    if result.summary:
        print(f"\n{result.summary.strip()}")
    if not result.ok:
        print("\nErreurs restantes :\n" + "\n".join(result.errors))
    print(f"\nTrace complète : {log}")

    if result.ok and sys.stdin.isatty():
        if input("\nGarder cet exemple pour le dataset de fine-tuning ? [o/N] ").strip().lower() in ("o", "oui", "y"):
            dataset.add(result)
            print(dataset.stats())
        print(f"Pour l'appliquer : myhypr apply {ws.root.name}")


def cmd_apply(args):
    ws = Workspace(WORKSPACES / args.workspace)
    files = ws.files()
    if not files:
        sys.exit(f"Rien à appliquer dans {ws.root}")
    errors = ws.validate_all()
    if errors and not args.force:
        sys.exit("La config ne passe pas la validation, abandon (--force pour forcer) :\n" + "\n".join(errors))

    print("Fichiers qui vont être copiés dans ~/.config :")
    for f in files:
        print(f"  {f}{'  (remplace l existant)' if (CONFIG_HOME / f).exists() else ''}")
    if not args.yes and input("Continuer ? [o/N] ").strip().lower() not in ("o", "oui", "y"):
        return

    backup = ops.apply(ws)
    print(f"Appliqué. Sauvegarde : {backup}\nPour revenir en arrière : myhypr restore {backup.name}")


def cmd_restore(args):
    try:
        for top in ops.restore(args.backup):
            print(f"Restauré : ~/.config/{top}")
    except FileNotFoundError as e:
        sys.exit(str(e))


def cmd_chat(args):
    from . import tui
    tui.main(args.model)


def cmd_dataset(_args):
    print(dataset.stats())


def main():
    p = argparse.ArgumentParser(prog="myhypr", description="Agent IA local pour configs Hyprland / Waybar")
    p.set_defaults(func=cmd_chat, model=CHAT_MODEL)
    sub = p.add_subparsers()

    s = sub.add_parser("chat", help="interface conversationnelle (défaut)")
    s.add_argument("--model", default=CHAT_MODEL)
    s.set_defaults(func=cmd_chat)

    s = sub.add_parser("index", help="indexe la doc (wiki Hyprland, Waybar, stubs Lua, docs/examples)")
    s.set_defaults(func=cmd_index)

    s = sub.add_parser("search", help="teste la recherche dans la doc")
    s.add_argument("query")
    s.add_argument("-k", type=int, default=5)
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("ask", help="demande une config à l'agent")
    s.add_argument("request")
    s.add_argument("--from-current", nargs="*", metavar="DIR",
                   help="part d'une copie de ta config actuelle, ex. --from-current hypr waybar")
    s.add_argument("--think", action="store_true", help="active le raisonnement de qwen (plus lent)")
    s.add_argument("--model", default=CHAT_MODEL)
    s.set_defaults(func=cmd_ask)

    s = sub.add_parser("apply", help="copie un dossier de travail validé dans ~/.config (avec sauvegarde)")
    s.add_argument("workspace", help="nom du dossier dans workspaces/")
    s.add_argument("-y", "--yes", action="store_true")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_apply)

    s = sub.add_parser("restore", help="restaure une sauvegarde de data/backups/")
    s.add_argument("backup")
    s.set_defaults(func=cmd_restore)

    s = sub.add_parser("dataset", help="statistiques du dataset de fine-tuning")
    s.set_defaults(func=cmd_dataset)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
