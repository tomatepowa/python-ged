"""Démarrage du serveur (« ged serveur ») : l'assistant d'installation tant que la GED n'est pas
installée, puis le serveur de la GED, dans le même processus."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import uvicorn

from ged import parametres
from ged.config import PORT_DEFAUT, NonConfiguree, charger, fichier_connexion, lire_connexion
from ged.db import connecter, initialiser

# Délai entre l'enregistrement de l'installation et l'arrêt de l'assistant : la page de fin et
# sa feuille de style ont le temps d'arriver
DELAI_ARRET = 1.5


def installee(fichier: Path) -> bool:
    """La GED est installée : connexion.toml existe et l'assistant a été mené à son terme.

    Une base injoignable arrête tout (PostgreSQL pas encore démarré, par exemple) : ce n'est pas
    une raison de relancer l'assistant.
    """
    connexion = lire_connexion(fichier)
    if connexion is None:
        return False
    initialiser(connexion.url)
    with connecter(connexion.url) as conn:
        return parametres.est_installee(conn)


def lancer_assistant(fichier: Path) -> None:
    from ged.web.assistant import creer_app_assistant

    connexion = lire_connexion(fichier)
    port = connexion.port if connexion else PORT_DEFAUT
    serveur: uvicorn.Server | None = None

    def terminer() -> None:
        threading.Timer(DELAI_ARRET, lambda: setattr(serveur, "should_exit", True)).start()

    app = creer_app_assistant(fichier, terminer)
    etat = app.state.assistant
    # Ce poste seulement, quel que soit le réglage d'écoute : l'assistant n'a pas d'identification
    serveur = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    print(
        "\n  Assistant d'installation de la GED\n"
        f"  Ouvrez http://localhost:{port}/ dans un navigateur, sur ce serveur.\n"
        f"  Code d'installation : {etat['code']}\n"
        "  (Ctrl+C pour abandonner)\n",
        flush=True,
    )
    serveur.run()
    if not etat["termine"]:
        sys.exit("Installation abandonnée : rien n'a été enregistré.")


def lancer(chemin: str | Path | None = None, assistant: bool = False) -> None:
    from ged.web.app import creer_app

    fichier = fichier_connexion(chemin)
    if assistant or not installee(fichier):
        lancer_assistant(fichier)
    try:
        cfg = charger(fichier)
    except NonConfiguree as exc:
        sys.exit(str(exc))
    # localhost plutôt que 127.0.0.1 : le navigateur n'y envoie la session Windows que sous ce nom
    hote = "localhost" if cfg.hote in ("127.0.0.1", "0.0.0.0") else cfg.hote
    print(f"Interface disponible sur http://{hote}:{cfg.port}", flush=True)
    uvicorn.run(creer_app(cfg, fichier_connexion=fichier), host=cfg.hote, port=cfg.port)
