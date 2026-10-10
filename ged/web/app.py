"""Application web de recherche (lecture seule)."""

from __future__ import annotations

import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from ged import config as configuration
from ged.recherche import PAR_PAGE, Filtres, facettes, rechercher, statistiques, suggerer

ICI = Path(__file__).parent
templates = Jinja2Templates(directory=ICI / "templates")

# Formats que le navigateur sait afficher : ouverts dans l'onglet plutôt que téléchargés
AFFICHABLES = {".pdf", ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".txt", ".log"}


def taille_lisible(octets: int) -> str:
    for unite in ("o", "Ko", "Mo", "Go"):
        if octets < 1024 or unite == "Go":
            return f"{octets:.0f} {unite}" if unite == "o" else f"{octets:.1f} {unite}".replace(".", ",")
        octets /= 1024
    return ""


templates.env.filters["taille"] = taille_lisible
templates.env.filters["date"] = lambda d: d.astimezone().strftime("%d/%m/%Y") if d else ""


def creer_app(cfg: configuration.Config | None = None) -> FastAPI:
    cfg = cfg or configuration.charger()
    pool = ConnectionPool(cfg.url_base, kwargs={"row_factory": dict_row}, min_size=1, max_size=10, open=False)

    @asynccontextmanager
    async def cycle_de_vie(app: FastAPI):
        pool.open()
        yield
        pool.close()

    app = FastAPI(title="GED DSI", lifespan=cycle_de_vie, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=ICI / "static"), name="static")

    def contexte_resultats(filtres: Filtres, conn) -> dict:
        resultats = rechercher(conn, filtres)
        return {
            "filtres": filtres,
            "resultats": resultats,
            "nb_pages": max(1, -(-resultats.total // PAR_PAGE)),
        }

    @app.get("/", response_class=HTMLResponse)
    def accueil(
        request: Request,
        q: str = "",
        pole: str = "",
        famille: str = "",
        tag: Annotated[list[int], Query()] = [],
        page: int = 1,
    ):
        filtres = Filtres(q=q, pole=pole, famille=famille, tags=tag, page=max(page, 1))
        with pool.connection() as conn:
            contexte = contexte_resultats(filtres, conn)
            choix = facettes(conn)
        tags_par_categorie: dict[str, list[dict]] = {}
        for t in choix["tags"]:
            tags_par_categorie.setdefault(t["categorie"], []).append(t)
        return templates.TemplateResponse(
            request,
            "recherche.html",
            {**contexte, "choix": choix, "tags_par_categorie": tags_par_categorie},
        )

    @app.get("/resultats", response_class=HTMLResponse)
    def resultats_seuls(
        request: Request,
        q: str = "",
        pole: str = "",
        famille: str = "",
        tag: Annotated[list[int], Query()] = [],
        page: int = 1,
    ):
        """Fragment HTML de la liste de résultats, pour la recherche pendant la frappe."""
        filtres = Filtres(q=q, pole=pole, famille=famille, tags=tag, page=max(page, 1))
        with pool.connection() as conn:
            contexte = contexte_resultats(filtres, conn)
        return templates.TemplateResponse(request, "_resultats.html", contexte)

    @app.get("/suggestions")
    def suggestions(q: str = ""):
        """Suggestions de la liste déroulante sous la barre de recherche (JSON)."""
        with pool.connection() as conn:
            return suggerer(conn, q[:200])

    @app.get("/document/{document_id}", response_class=HTMLResponse)
    def detail(request: Request, document_id: int):
        with pool.connection() as conn:
            doc = conn.execute(
                "SELECT d.*, s.nom AS source, s.pole FROM document d JOIN source s ON s.id = d.source_id "
                "WHERE d.id = %s",
                (document_id,),
            ).fetchone()
            if not doc:
                raise HTTPException(404, "Document inconnu")
            tags = conn.execute(
                "SELECT t.categorie, t.libelle, dt.origine FROM document_tag dt JOIN tag t ON t.id = dt.tag_id "
                "WHERE dt.document_id = %s ORDER BY t.categorie, t.libelle",
                (document_id,),
            ).fetchall()
            doublons = conn.execute(
                "SELECT id, chemin_relatif FROM document "
                "WHERE empreinte = %s AND id <> %s AND supprime_le IS NULL ORDER BY chemin_relatif",
                (doc["empreinte"], document_id),
            ).fetchall() if doc["empreinte"] else []
        return templates.TemplateResponse(
            request, "document.html", {"doc": doc, "tags": tags, "doublons": doublons}
        )

    @app.get("/document/{document_id}/fichier")
    def fichier(document_id: int):
        """Sert le fichier d'origine. Seuls les chemins présents dans l'index sont accessibles."""
        with pool.connection() as conn:
            doc = conn.execute(
                "SELECT chemin, nom, extension FROM document WHERE id = %s AND supprime_le IS NULL",
                (document_id,),
            ).fetchone()
        if not doc:
            raise HTTPException(404, "Document inconnu")
        chemin = Path(doc["chemin"])
        if not chemin.is_file():
            raise HTTPException(410, "Le fichier n'existe plus à cet emplacement (réindexation en attente)")
        type_mime = mimetypes.guess_type(doc["nom"])[0] or "application/octet-stream"
        disposition = "inline" if doc["extension"] in AFFICHABLES else "attachment"
        return FileResponse(chemin, media_type=type_mime, filename=doc["nom"], content_disposition_type=disposition)

    @app.get("/stats", response_class=HTMLResponse)
    def stats(request: Request):
        with pool.connection() as conn:
            donnees = statistiques(conn)
        return templates.TemplateResponse(request, "stats.html", donnees)

    return app
