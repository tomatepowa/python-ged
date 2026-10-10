"""Ligne de commande : `ged <commande>` (ou `python -m ged <commande>`)."""

from __future__ import annotations

import argparse
import logging
import sys

from ged import config as configuration


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ged", description="GED DSI : indexation et recherche documentaire.")
    parser.add_argument("-c", "--config", help="fichier de configuration (défaut : config.toml ou $GED_CONFIG)")
    parser.add_argument("-v", "--verbeux", action="store_true", help="journal détaillé")
    commandes = parser.add_subparsers(dest="commande", required=True)

    commandes.add_parser("init-db", help="crée ou met à jour le schéma de la base")

    p_indexer = commandes.add_parser("indexer", help="parcourt les sources et met à jour l'index")
    p_indexer.add_argument("--forcer", action="store_true", help="relit tous les fichiers, même inchangés")
    p_indexer.add_argument("--source", action="append", help="limiter à cette source (répétable)")

    p_ocr = commandes.add_parser("ocr", help="passe en OCR les documents scannés en attente")
    p_ocr.add_argument("--limite", type=int, help="nombre maximum de documents à traiter")

    commandes.add_parser("serveur", help="lance l'interface web de recherche")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbeux else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = configuration.charger(args.config)

    if args.commande == "init-db":
        from ged.db import initialiser

        initialiser(cfg.url_base)
        print("Schéma à jour.")

    elif args.commande == "indexer":
        from ged.db import connecter
        from ged.indexeur import Indexeur

        with connecter(cfg.url_base) as conn:
            bilan = Indexeur(cfg, conn).indexer(forcer=args.forcer, noms_sources=args.source)
        _afficher_bilan("Indexation terminée", bilan)

    elif args.commande == "ocr":
        from ged.db import connecter
        from ged.indexeur import Indexeur

        if not cfg.ocr_active:
            sys.exit("OCR désactivé : passez [ocr] active = true dans config.toml.")
        with connecter(cfg.url_base) as conn:
            bilan = Indexeur(cfg, conn).ocr(limite=args.limite)
        _afficher_bilan("OCR terminé", bilan)

    elif args.commande == "serveur":
        import uvicorn

        from ged.web.app import creer_app

        print(f"Interface disponible sur http://{cfg.hote}:{cfg.port}")
        uvicorn.run(creer_app(cfg), host=cfg.hote, port=cfg.port)


def _afficher_bilan(titre: str, bilan) -> None:
    libelles = {
        "indexe": "texte indexé",
        "ocr_requis": "scans en attente d'OCR",
        "non_supporte": "formats non lus",
        "erreur": "erreurs de lecture",
        "inchange": "inchangés (non relus)",
        "supprime": "disparus de la source",
        "illisible": "illisibles (droits ?)",
        "sources_inaccessibles": "sources inaccessibles",
    }
    print(f"\n{titre} :")
    for cle, nb in sorted(bilan.items(), key=lambda x: -x[1]):
        print(f"  {nb:>7}  {libelles.get(cle, cle)}")


if __name__ == "__main__":
    main()
