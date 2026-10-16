"""Commandes de la ligne de commande (« ged <commande> ») : définition et aide.

La même définition sert à la ligne de commande (ged.__main__) et à la page d'aide « Commandes du
serveur » des administrateurs : l'aide ne peut pas s'écarter de ce que les commandes acceptent.
`help` : le but en une ligne ; `description` : ce que fait la commande, et quand la lancer.
"""

from __future__ import annotations

import argparse


def construire_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ged", description="GED DSI : indexation et recherche documentaire.")
    parser.add_argument("-c", "--connexion",
                        help="fichier de connexion à la base (défaut : $GED_CONNEXION, sinon ./connexion.toml)")
    parser.add_argument("-v", "--verbeux", action="store_true", help="journal détaillé")
    commandes = parser.add_subparsers(dest="commande", required=True, metavar="commande")

    p_serveur = commandes.add_parser(
        "serveur", help="lance l'interface web (l'assistant d'installation tant que la GED n'est pas installée)",
        description="Sert l'interface de recherche. Au premier lancement, ouvre d'abord l'assistant d'installation "
                    "(sur ce poste seulement, avec un code affiché dans la console), puis démarre la GED. En "
                    "production, à lancer comme service Windows pour qu'il tourne en permanence.",
    )
    p_serveur.add_argument("--assistant", action="store_true",
                           help="relance l'assistant d'installation pour corriger connexion ou réglages "
                                "(par exemple des administrateurs mal saisis) ; arrêter d'abord le serveur")

    p_preparer = commandes.add_parser(
        "preparer", help="parcours rapide des sources et page pour choisir les dossiers à exclure (avant d'indexer)",
        description="Parcourt les sources sans ouvrir aucun fichier (noms, tailles, dates) et ouvre une page sur ce "
                    "poste avec l'arborescence : on décoche les dossiers sans intérêt (applications, archives...), "
                    "et les choix sont enregistrés dans les réglages. Avant la première indexation, ou après l'ajout "
                    "d'une source.",
    )
    p_preparer.add_argument("--port", type=int, default=8001, help="port de la page (défaut : 8001)")
    p_preparer.add_argument("--sans-navigateur", action="store_true", help="ne pas ouvrir la page automatiquement")

    commandes.add_parser(
        "verifier", help="vérifie l'installation : base, sources, outils, identification",
        description="Contrôle la base, l'accès aux sources et à leurs droits, les exclusions, la taxonomie, l'OCR, "
                    "LibreOffice, le port de l'interface et les administrateurs ; lit les droits d'un échantillon de "
                    "fichiers sans les ouvrir. Après l'installation ou un changement de réglages. Code retour 1 en "
                    "cas d'erreur.",
    )

    p_indexer = commandes.add_parser(
        "indexer", help="parcourt les sources et met à jour l'index",
        description="Lit les fichiers nouveaux ou modifiés (texte, propriétés, code documentaire), relit les droits "
                    "de tous, reconnaît les fichiers déplacés et retire de l'index ceux qui ont disparu. Les fichiers "
                    "ne sont jamais modifiés. Chaque nuit, en tâche planifiée avec le compte de service.",
    )
    p_indexer.add_argument("--forcer", action="store_true", help="relit tous les fichiers, même inchangés")
    p_indexer.add_argument("--source", action="append", help="limiter à cette source (répétable)")

    p_ocr = commandes.add_parser(
        "ocr", help="passe en OCR les documents scannés en attente",
        description="Lit le texte des PDF scannés et des images repérés par l'indexation ; le « texte » des photos "
                    "sans écriture est écarté. Long (quelques secondes par page) : après « ged indexer », la nuit. "
                    "Ctrl+C l'arrête sans rien perdre.",
    )
    p_ocr.add_argument("--limite", type=int, help="nombre maximum de documents à traiter")
    p_ocr.add_argument("--reessayer", action="store_true",
                       help="relance aussi les documents dont l'OCR a échoué (fichier verrouillé, interruption...)")

    commandes.add_parser(
        "recalculer", help="réapplique tags, codes et filtre OCR sans relire les fichiers (après modif. de la taxonomie)",
        description="Recalcule les tags automatiques, les codes documentaires et le filtre des textes OCR illisibles "
                    "à partir du texte déjà en base. Rarement utile : la page Tags recalcule d'elle-même après une "
                    "modification.",
    )

    p_exporter = commandes.add_parser(
        "exporter-taxonomie", help="écrit la taxonomie de la base (mots-clés des tags) dans un fichier TOML",
        description="Écrit les tags et leurs mots-clés dans un fichier, pour les sauvegarder, les versionner ou les "
                    "retravailler (même chose que le bouton Exporter de la page Tags).",
    )
    p_exporter.add_argument("--sortie", help="fichier produit (défaut : taxonomie.toml)")

    p_importer = commandes.add_parser(
        "importer-taxonomie", help="remplace la taxonomie de la base par celle d'un fichier TOML, puis recalcule les tags",
        description="Affiche les différences avec la taxonomie en base et demande confirmation ; les tags absents du "
                    "fichier perdent leurs mots-clés mais restent. Les tags sont ensuite recalculés.",
    )
    p_importer.add_argument("--fichier", help="fichier lu (défaut : taxonomie.toml)")
    p_importer.add_argument("--oui", action="store_true", help="sans demander de confirmation")
    p_importer.add_argument("--sans-recalcul", action="store_true", help="ne pas recalculer les tags ensuite")

    p_analyser = commandes.add_parser(
        "analyser", help="rapport pour construire la taxonomie (dossiers, mots fréquents, couverture des tags)",
        description="Écrit un rapport Markdown : dossiers récurrents, mots fréquents des noms de fichiers, documents "
                    "sans tag. Sert à compléter la taxonomie. À relire avant de le partager : il peut contenir des "
                    "noms de personnes.",
    )
    p_analyser.add_argument("--sortie", default="analyse-taxonomie.md", help="fichier Markdown produit")
    p_analyser.add_argument("--min", type=int, default=3, dest="min_docs",
                            help="nombre minimal de documents pour qu'un terme apparaisse (défaut : 3)")

    p_reinit = commandes.add_parser(
        "reinitialiser", help="vide l'index pour repartir de zéro (les fichiers indexés ne sont pas touchés)",
        description="Vide documents, textes et tags posés à la main (perdus, le nombre est annoncé) ; garde la "
                    "taxonomie, les réglages et le journal. Confirmation demandée. Ensuite : « ged indexer ».",
    )
    p_reinit.add_argument("--oui", action="store_true", help="sans demander de confirmation")

    commandes.add_parser(
        "init-db", help="crée ou met à jour le schéma de la base",
        description="Crée les tables et index s'ils manquent. Rarement utile : l'assistant et chaque commande le font "
                    "à leur démarrage.",
    )
    return parser


def _options(parser: argparse.ArgumentParser) -> list[dict]:
    return [
        {"noms": ", ".join(action.option_strings), "aide": action.help or ""}
        for action in parser._actions
        if action.option_strings and not isinstance(action, argparse._HelpAction)
    ]


def aide() -> dict:
    """Aide des commandes, pour la page « Commandes du serveur » :
    {"options": [...], "commandes": [{"nom", "but", "detail", "options": [{"noms", "aide"}]}]}."""
    parser = construire_parser()
    sous = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    buts = {choix.dest: choix.help for choix in sous._choices_actions}
    return {
        "options": _options(parser),
        "commandes": [
            {"nom": nom, "but": buts.get(nom, ""), "detail": p.description or "", "options": _options(p)}
            for nom, p in sous.choices.items()
        ],
    }
