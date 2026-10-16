"""Page de préparation (« ged preparer ») : cocher les dossiers à indexer avant la première indexation.

Servie sur ce poste seulement (127.0.0.1), sans base de données ni identification : elle ne fait
qu'afficher le parcours rapide des sources et enregistrer les dossiers exclus dans les réglages.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ged.config import Config
from ged.indexeur import dossier_exclu
from ged.preparation import (
    Dossier, alertes, exclusions_minimales, fichiers_indexes, motifs_exclusions, motifs_orphelins,
)
from ged.web.app import taille_lisible

ICI = Path(__file__).parent
templates = Jinja2Templates(directory=ICI / "templates")
templates.env.filters["taille"] = taille_lisible
templates.env.filters["nombre"] = lambda n: f"{n:,}".replace(",", " ")


def niveau(fichiers: int, total: int) -> int:
    """Poids d'un dossier dans sa source, pour la couleur de son compteur (0 à 3)."""
    part = fichiers / total if total else 0
    return 3 if part >= 0.2 else 2 if part >= 0.05 else 1 if part >= 0.01 else 0


def creer_app_preparation(cfg: Config, sources: list[tuple[str, Dossier]],
                          enregistrer: Callable[[list[str]], None]) -> FastAPI:
    """`sources` : résultat de ged.preparation.parcourir_sources ; `enregistrer(motifs)` : écrit
    les dossiers exclus dans les réglages."""
    # Jeton du formulaire : une autre page ouverte dans le navigateur ne peut pas valider à notre place
    jeton = secrets.token_urlsafe(16)
    etat = {"exclure": list(cfg.exclure), "prepares": list(cfg.exclure_prepares)}
    par_id = {d.id: d for _, arbre in sources for d in arbre.tous()}

    def verrouille(d: Dossier) -> bool:
        """Exclu par un motif écrit dans la page Réglages : seule sa suppression le réintègre."""
        return dossier_exclu(d.chemin, cfg.exclure_manuels)

    def vue(d: Dossier, total: int, alertes_parent: dict) -> dict:
        toutes = alertes(d)
        return {
            "coche": not dossier_exclu(d.chemin, etat["exclure"]),
            "verrouille": verrouille(d),
            "niveau": niveau(d.fichiers, total),
            "toutes_alertes": toutes,
            # Une alerte qui vaut pour tout le contenu (ancienneté, programmes...) n'est montrée que
            # sur le dossier le plus haut ; celle sur le nom reste propre à chaque dossier
            "alertes": [texte for genre, texte in toutes.items() if genre == "nom" or genre not in alertes_parent],
            "annee": datetime.fromtimestamp(d.recent).year if d.recent else None,
        }

    templates.env.globals["vue"] = vue

    app = FastAPI(title="GED DSI : préparation", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=ICI / "static"), name="static")

    @app.get("/", response_class=HTMLResponse)
    def page(request: Request):
        return templates.TemplateResponse(
            request, "preparation.html", {"sources": sources, "jeton": jeton}
        )

    @app.post("/valider", response_class=HTMLResponse)
    async def valider(request: Request):
        # Formulaire lu à la main : pas besoin de la dépendance python-multipart pour une seule page
        champs = parse_qs((await request.body()).decode("utf-8"))
        if champs.get("jeton", [""])[0] != jeton:
            raise HTTPException(403, "Formulaire expiré : rechargez la page.")
        gardes = {int(v) for v in champs.get("garder", []) if v.isdigit()}
        # La racine d'une source n'a pas de case : on retire une source dans la page Réglages
        gardes |= {arbre.id for _, arbre in sources}
        decoches = [d.chemin for d in par_id.values() if d.id not in gardes and not verrouille(d)]
        chemins = set(exclusions_minimales(decoches))
        exclus = [d for d in par_id.values() if d.chemin in chemins]
        exclus.sort(key=lambda d: str(d.chemin).lower())
        # Motifs de l'ancien fichier qui ne visent aucun dossier parcouru : gardés, plutôt que perdus
        conserves = motifs_orphelins(etat["prepares"], sources)
        etat["prepares"] = motifs_exclusions(exclus, conserves)
        enregistrer(etat["prepares"])
        etat["exclure"] = cfg.exclure_manuels + etat["prepares"]
        return templates.TemplateResponse(
            request,
            "preparation_validee.html",
            {
                "exclus": exclus, "conserves": conserves, "nb_manuels": len(cfg.exclure_manuels),
                "total": sum(arbre.fichiers for _, arbre in sources),
                # Avec les motifs de la page Réglages : le nombre que l'indexation lira vraiment
                "indexes": sum(fichiers_indexes(arbre, etat["exclure"]) for _, arbre in sources),
            },
        )

    return app
