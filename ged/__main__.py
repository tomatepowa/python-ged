"""Ligne de commande : `ged <commande>` (ou `python -m ged <commande>`)."""

from __future__ import annotations

import logging
import sys

from ged import config as configuration
from ged.commandes import construire_parser
from ged.progression import installer_filtre


def main(argv: list[str] | None = None) -> None:
    args = construire_parser().parse_args(argv)
    # Sortie redirigée sous Windows (cp1252) : un caractère non représentable (« − », « → ») est
    # remplacé au lieu d'interrompre la commande
    for flux in (sys.stdout, sys.stderr):
        if hasattr(flux, "reconfigure"):
            flux.reconfigure(errors="replace")
    logging.basicConfig(
        level=logging.DEBUG if args.verbeux else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    # Les messages du journal s'écrivent sur leur propre ligne, au-dessus de la barre de progression
    installer_filtre()
    if args.commande == "serveur":
        from ged.web.lancement import lancer

        lancer(args.connexion, assistant=args.assistant)
        return

    if args.commande == "init-db":
        from ged.db import initialiser

        connexion = configuration.lire_connexion(args.connexion)
        if connexion is None:
            sys.exit("Pas de connexion.toml : lancez « ged serveur » pour suivre l'assistant d'installation.")
        initialiser(connexion.url)
        print("Schéma à jour.")
        return

    try:
        cfg = configuration.charger(args.connexion)
    except configuration.NonConfiguree as exc:
        sys.exit(str(exc))

    if args.commande == "preparer":
        import webbrowser

        import uvicorn

        from ged.preparation import parcourir_sources
        from ged.web.preparation import creer_app_preparation

        from ged import parametres
        from ged.db import connecter

        def enregistrer(motifs: list[str]) -> None:
            with connecter(cfg.url_base) as conn:
                parametres.ecrire(conn, {"exclure_prepares": motifs}, "ged preparer")

        sources = parcourir_sources(cfg)
        adresse = f"http://127.0.0.1:{args.port}"
        print(f"\nPage de préparation : {adresse}  (Ctrl+C pour arrêter)")
        if not args.sans_navigateur:
            webbrowser.open(adresse)
        # Ce poste seulement : la page modifie les réglages, sans identification
        uvicorn.run(creer_app_preparation(cfg, sources, enregistrer), host="127.0.0.1", port=args.port,
                    log_level="warning")

    elif args.commande == "verifier":
        from ged.verification import afficher, verifier

        if not afficher(verifier(cfg)):
            sys.exit(1)

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
            sys.exit("OCR désactivé : activez-le dans la page Réglages de l'interface.")
        with connecter(cfg.url_base) as conn:
            bilan = Indexeur(cfg, conn).ocr(limite=args.limite, reessayer=args.reessayer)
        _afficher_bilan("OCR terminé", bilan)

    elif args.commande == "recalculer":
        from ged.db import connecter
        from ged.indexeur import Indexeur
        from ged.recalcul_tags import RecalculEnCours, verrou_recalcul

        with connecter(cfg.url_base) as conn:
            try:
                with verrou_recalcul(conn):
                    bilan = Indexeur(cfg, conn).recalculer()
            except RecalculEnCours as exc:
                sys.exit(f"{exc} Relancez une fois qu'il est terminé.")
        _afficher_bilan("Recalcul terminé", bilan)

    elif args.commande == "exporter-taxonomie":
        from pathlib import Path

        from ged.db import connecter
        from ged.taxonomie_base import exporter

        sortie = Path(args.sortie) if args.sortie else cfg.taxonomie
        with connecter(cfg.url_base) as conn:
            sortie.write_text(exporter(conn), encoding="utf-8")
        print(f"Taxonomie exportée dans {sortie.resolve()}.")

    elif args.commande == "importer-taxonomie":
        from pathlib import Path

        from ged.db import connecter
        from ged.recalcul_tags import RecalculEnCours, verrou_recalcul
        from ged.taxonomie import lire_toml
        from ged.taxonomie_base import differences, importer, recalculer_tous

        fichier = Path(args.fichier) if args.fichier else cfg.taxonomie
        if not fichier.is_file():
            sys.exit(f"Fichier introuvable : {fichier.resolve()}")
        try:
            nouvelle = lire_toml(fichier.read_text(encoding="utf-8"))
        except ValueError as exc:
            sys.exit(f"{fichier} illisible : {exc}")
        with connecter(cfg.url_base) as conn:
            changements = differences(conn, nouvelle)
            if not changements:
                print("La base a déjà cette taxonomie : rien à importer.")
                return
            print(f"Import de {fichier.resolve()} :")
            for ligne in changements:
                print(f"  {ligne}")
            if not args.oui and input("Tapez OUI pour importer : ").strip() != "OUI":
                sys.exit("Abandon : rien n'a été modifié.")
            importer(conn, nouvelle, f"import de {fichier.name}")
            conn.commit()
            print(f"Taxonomie importée ({len(changements)} changements).")
            if args.sans_recalcul:
                print("Tags non recalculés : lancez « ged recalculer » ou « Recalculer tout » dans la page Tags.")
                return
            try:
                with verrou_recalcul(conn):
                    bilan = recalculer_tous(conn)
            except RecalculEnCours as exc:
                sys.exit(f"{exc} Recalculez les tags une fois qu'il est terminé.")
        print(f"Tags recalculés sur {bilan['examines']} documents.")

    elif args.commande == "analyser":
        from pathlib import Path

        from ged.analyse import charger, rapport
        from ged.db import connecter
        from ged.taxonomie_base import charger as charger_taxonomie

        with connecter(cfg.url_base) as conn:
            documents, comptes = charger(conn)
            taxonomie = charger_taxonomie(conn)
        texte = rapport(documents, taxonomie, comptes, min_docs=args.min_docs)
        Path(args.sortie).write_text(texte, encoding="utf-8")
        print(f"Rapport écrit dans {Path(args.sortie).resolve()} ({len(documents)} documents analysés).")
        print("Relisez-le avant de le partager : il peut contenir des noms de personnes ou de projets.")

    elif args.commande == "reinitialiser":
        from ged.db import connecter, vider_index
        from ged.tags_manuels import nb_poses_a_la_main

        with connecter(cfg.url_base) as conn:
            nb = conn.execute(
                "SELECT (SELECT count(*) FROM document WHERE supprime_le IS NULL) AS documents, (SELECT count(*) FROM source) AS sources"
            ).fetchone()
            print(f"L'index contient {nb['documents']} documents ({nb['sources']} sources).")
            main = nb_poses_a_la_main(conn)
            if any(main.values()):
                print(f"Le travail fait à la main sera perdu : {main['manuels']} tags posés sur des documents, "
                      f"{main['retires']} tags retirés, {main['dossiers']} tags de dossiers.")
            print("Il sera entièrement vidé (la taxonomie est gardée) ; les fichiers des partages ne sont pas touchés.")
            if not args.oui and input("Tapez OUI pour confirmer : ").strip() != "OUI":
                sys.exit("Abandon : rien n'a été modifié.")
            vider_index(conn)
            conn.commit()
        print("Index vidé. Relancez l'indexation (ged indexer) pour le reconstruire.")


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
