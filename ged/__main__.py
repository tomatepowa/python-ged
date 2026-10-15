"""Ligne de commande : `ged <commande>` (ou `python -m ged <commande>`)."""

from __future__ import annotations

import argparse
import logging
import sys

from ged import config as configuration
from ged.progression import installer_filtre


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ged", description="GED DSI : indexation et recherche documentaire.")
    parser.add_argument("-c", "--config", help="fichier de configuration (défaut : config.toml ou $GED_CONFIG)")
    parser.add_argument("-v", "--verbeux", action="store_true", help="journal détaillé")
    commandes = parser.add_subparsers(dest="commande", required=True)

    commandes.add_parser("init-db", help="crée ou met à jour le schéma de la base")

    p_preparer = commandes.add_parser(
        "preparer", help="parcours rapide des sources et page pour choisir les dossiers à exclure (avant d'indexer)"
    )
    p_preparer.add_argument("--port", type=int, default=8001, help="port de la page (défaut : 8001)")
    p_preparer.add_argument("--sans-navigateur", action="store_true", help="ne pas ouvrir la page automatiquement")

    commandes.add_parser("verifier", help="vérifie l'installation : base, sources, outils, identification")

    p_indexer = commandes.add_parser("indexer", help="parcourt les sources et met à jour l'index")
    p_indexer.add_argument("--forcer", action="store_true", help="relit tous les fichiers, même inchangés")
    p_indexer.add_argument("--source", action="append", help="limiter à cette source (répétable)")

    p_ocr = commandes.add_parser("ocr", help="passe en OCR les documents scannés en attente")
    p_ocr.add_argument("--limite", type=int, help="nombre maximum de documents à traiter")

    commandes.add_parser(
        "recalculer", help="réapplique tags, codes et filtre OCR sans relire les fichiers (après modif. de taxonomie.toml)"
    )

    commandes.add_parser("serveur", help="lance l'interface web de recherche")

    p_analyser = commandes.add_parser(
        "analyser", help="rapport pour construire la taxonomie (dossiers, mots fréquents, couverture des tags)"
    )
    p_analyser.add_argument("--sortie", default="analyse-taxonomie.md", help="fichier Markdown produit")
    p_analyser.add_argument("--min", type=int, default=3, dest="min_docs",
                            help="nombre minimal de documents pour qu'un terme apparaisse (défaut : 3)")

    p_reinit = commandes.add_parser(
        "reinitialiser", help="vide l'index pour repartir de zéro (les fichiers indexés ne sont pas touchés)"
    )
    p_reinit.add_argument("--oui", action="store_true", help="sans demander de confirmation")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbeux else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    # Les messages du journal s'écrivent sur leur propre ligne, au-dessus de la barre de progression
    installer_filtre()
    cfg = configuration.charger(args.config)

    if args.commande == "init-db":
        from ged.db import initialiser

        initialiser(cfg.url_base)
        print("Schéma à jour.")

    elif args.commande == "preparer":
        import webbrowser

        import uvicorn

        from ged.preparation import parcourir_sources
        from ged.web.preparation import creer_app_preparation

        sources = parcourir_sources(cfg)
        adresse = f"http://127.0.0.1:{args.port}"
        print(f"\nPage de préparation : {adresse}  (Ctrl+C pour arrêter)")
        if not args.sans_navigateur:
            webbrowser.open(adresse)
        # Ce poste seulement : la page écrit dans le dossier de la GED, sans identification
        uvicorn.run(creer_app_preparation(cfg, sources), host="127.0.0.1", port=args.port, log_level="warning")

    elif args.commande == "verifier":
        from ged.verification import afficher, verifier

        if not afficher(verifier(cfg)):
            sys.exit(1)

    elif args.commande == "indexer":
        from ged.db import connecter, initialiser
        from ged.indexeur import Indexeur

        # Schéma idempotent : une mise à jour du code qui ajoute des colonnes n'oblige pas à penser à init-db
        initialiser(cfg.url_base)
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

    elif args.commande == "recalculer":
        from ged.db import connecter
        from ged.indexeur import Indexeur

        with connecter(cfg.url_base) as conn:
            bilan = Indexeur(cfg, conn).recalculer()
        _afficher_bilan("Recalcul terminé", bilan)

    elif args.commande == "analyser":
        from pathlib import Path

        from ged.analyse import charger, rapport
        from ged.db import connecter
        from ged.taxonomie import Taxonomie

        with connecter(cfg.url_base) as conn:
            documents, comptes = charger(conn)
        texte = rapport(documents, Taxonomie.charger(cfg.taxonomie), comptes, min_docs=args.min_docs)
        Path(args.sortie).write_text(texte, encoding="utf-8")
        print(f"Rapport écrit dans {Path(args.sortie).resolve()} ({len(documents)} documents analysés).")
        print("Relisez-le avant de le partager : il peut contenir des noms de personnes ou de projets.")

    elif args.commande == "reinitialiser":
        from ged.db import connecter, initialiser, vider_index
        from ged.tags_manuels import nb_poses_a_la_main

        initialiser(cfg.url_base)
        with connecter(cfg.url_base) as conn:
            nb = conn.execute(
                "SELECT (SELECT count(*) FROM document WHERE supprime_le IS NULL) AS documents, (SELECT count(*) FROM source) AS sources"
            ).fetchone()
            print(f"L'index contient {nb['documents']} documents ({nb['sources']} sources).")
            main = nb_poses_a_la_main(conn)
            if any(main.values()):
                print(f"Le travail fait à la main sera perdu : {main['manuels']} tags posés sur des documents, "
                      f"{main['retires']} tags retirés, {main['dossiers']} tags de dossiers.")
            print("Il sera entièrement vidé ; les fichiers des partages ne sont pas touchés.")
            if not args.oui and input("Tapez OUI pour confirmer : ").strip() != "OUI":
                sys.exit("Abandon : rien n'a été modifié.")
            vider_index(conn)
            conn.commit()
        print("Index vidé. Relancez l'indexation (ged indexer) pour le reconstruire.")

    elif args.commande == "serveur":
        import uvicorn

        from ged.db import initialiser
        from ged.web.app import creer_app

        initialiser(cfg.url_base)
        # localhost plutôt que 127.0.0.1 : le navigateur n'y envoie la session Windows que sous ce nom
        hote = "localhost" if cfg.hote in ("127.0.0.1", "0.0.0.0") else cfg.hote
        print(f"Interface disponible sur http://{hote}:{cfg.port}")
        uvicorn.run(creer_app(cfg), host=cfg.hote, port=cfg.port)


def _afficher_bilan(titre: str, bilan) -> None:
    libelles = {
        "indexe": "texte indexé",
        "ocr_requis": "scans en attente d'OCR",
        "sans_texte": "images sans texte lisible",
        "recalcule": "documents recalculés",
        "ocr_ecarte": "textes OCR écartés (illisibles)",
        "vocabulaire": "mots dans le vocabulaire",
        "non_supporte": "formats non lus",
        "erreur": "erreurs de lecture",
        "inchange": "inchangés (non relus)",
        "droits_modifies": "inchangés, droits d'accès mis à jour",
        "deplace": "renommés ou déplacés (texte et tags conservés)",
        "supprime": "disparus de la source",
        "illisible": "illisibles (droits ?)",
        "sources_inaccessibles": "sources inaccessibles",
        "dossiers_inaccessibles": "dossiers inaccessibles (contenu conservé dans l'index)",
    }
    print(f"\n{titre} :")
    for cle, nb in sorted(bilan.items(), key=lambda x: -x[1]):
        print(f"  {nb:>7}  {libelles.get(cle, cle)}")


if __name__ == "__main__":
    main()
