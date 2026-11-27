"""Entry point: myhypr index | ask | apply | dataset."""

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


def cmd_eval(args):
    from . import evaluate
    if args.rescore:
        return evaluate.rescore(args.rescore)
    evaluate.main(args.filter, args.tag, args.model, args.think, args.repeat, not args.no_live, args.label,
                  "variants" if args.variants else "bench")


def cmd_migrate(args):
    from .migrate import migrate
    from .tools import verify_hyprland_file
    conf = Path(args.conf).expanduser()
    result = migrate(conf)
    out = Path(args.out).expanduser() if args.out else WORKSPACES / time.strftime("%Y%m%d-%H%M%S") / "hypr"
    for rel, text in result.files.items():
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_text(text)
    print(result.summary())
    print(f"\nÉcrit dans {out}\nValidation : {verify_hyprland_file(out / conf.with_suffix('.lua').name)}")
    print("Pour terminer avec l'agent : uv run myhypr  puis  /migrate")


def cmd_system(_args):
    from .system import detect, summary
    print(summary(detect()))


def cmd_candidates(_args):
    from . import candidates
    print(candidates.stats())
    print("Pour les examiner : uv run myhypr  puis  /candidates")


def cmd_setup(args):
    from . import setup

    def confirm(question: str) -> bool:
        if args.yes:
            return True
        try:
            return input(f"{question} [o/N] ").strip().lower() in ("o", "oui", "y", "yes")
        except EOFError:
            return False
    print("✓", setup.install_launcher())
    if args.bind:
        print("·", setup.install_bind(confirm))
    else:
        print("· raccourci Hyprland non installé (myhypr setup --bind pour l'ajouter)")


def cmd_doctor(_args):
    from . import setup
    icons = {"ok": "✓", "warn": "!", "err": "✗"}
    report = setup.doctor()
    for level, item, detail in report:
        print(f"  {icons[level]} {item:<24} {detail}")
    errs = sum(1 for lvl, _, _ in report if lvl == "err")
    warns = sum(1 for lvl, _, _ in report if lvl == "warn")
    print(f"\n{errs} erreur(s), {warns} avertissement(s)")
    sys.exit(1 if errs else 0)


def cmd_variants(args):
    from . import variants
    if args.action == "generate":
        variants.generate(args.paraphrases)
    else:
        cases = variants.load()
        kinds = {"template": 0, "paraphrase": 0}
        for c in cases:
            kinds["template" if "template" in c["tags"] else "paraphrase"] += 1
        print(f"{len(cases)} variantes ({kinds['template']} paramétrées, {kinds['paraphrase']} reformulations)")
        for c in cases[:args.show]:
            print(f"  {c['id']:<26} {c['request'][:90]}")
        print("Jouer : uv run myhypr eval --variants   (puis /candidates pour les revoir)")


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

    s = sub.add_parser("eval", help="banc d'évaluation (evals/cases.toml)")
    s.add_argument("filter", nargs="?", help="regex sur l'id des cas, ex. '^wb-'")
    s.add_argument("--tag", help="ne lancer que les cas avec ce tag (waybar, hypr, modify…)")
    s.add_argument("--model", default=CHAT_MODEL)
    s.add_argument("--think", action="store_true")
    s.add_argument("-n", "--repeat", type=int, default=1, help="répéter chaque cas (mesure la variance)")
    s.add_argument("--no-live", action="store_true", help="ne pas lancer waybar pour valider (plus rapide)")
    s.add_argument("--label", default="", help="étiquette du run, ex. 'avant-exemples'")
    s.add_argument("--rescore", metavar="RESULTS.json", help="recalcule les vérifications d'un run existant")
    s.add_argument("--variants", action="store_true",
                   help="joue les variantes (evals/variants/generated.json) au lieu du banc : de nouveaux candidats "
                        "pour le dataset, sans toucher à la référence")
    s.set_defaults(func=cmd_eval)

    s = sub.add_parser("migrate", help="convertit un hyprland.conf (hyprlang) en Lua, sans IA, dans un dossier de travail")
    s.add_argument("conf", nargs="?", default="~/.config/hypr/hyprland.conf")
    s.add_argument("--out", help="dossier de sortie (défaut : workspaces/<date>/hypr)")
    s.set_defaults(func=cmd_migrate)

    s = sub.add_parser("system", help="ce que l'agent détecte de la machine")
    s.set_defaults(func=cmd_system)

    s = sub.add_parser("candidates", help="candidats du banc pour le dataset (revue : /candidates dans l'UI)")
    s.set_defaults(func=cmd_candidates)

    s = sub.add_parser("setup", help="lanceur, entrée de menu et (option) raccourci Hyprland")
    s.add_argument("--bind", action="store_true", help="ajoute un raccourci (première combinaison libre)")
    s.add_argument("-y", "--yes", action="store_true", help="sans confirmation")
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("doctor", help="vérifie l'installation (dépendances, modèles, docs, index)")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("variants", help="variantes des demandes du banc pour diversifier le dataset")
    s.add_argument("action", nargs="?", choices=["generate", "list"], default="list")
    s.add_argument("--paraphrases", type=int, default=2, help="reformulations par demande du banc (max 4)")
    s.add_argument("--show", type=int, default=15)
    s.set_defaults(func=cmd_variants)

    s = sub.add_parser("dataset", help="statistiques du dataset de fine-tuning")
    s.set_defaults(func=cmd_dataset)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
