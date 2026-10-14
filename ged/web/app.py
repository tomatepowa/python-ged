"""Application web de recherche (lecture seule)."""

from __future__ import annotations

import logging
import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from ged import config as configuration
from ged.acces import FiltreAcces, Lecteur, filtre_acces
from ged.arborescence import construire_arbre_resultats, dossier_parent, fil_ariane
from ged.recherche import DOCUMENTS_ARBRE_MAX, PAR_PAGE, Filtres, facettes, rechercher, statistiques, suggerer
from ged.web.authentification import (
    COOKIE, Authentification, AuthentificationRefusee, Negociateur, NegociateurSSPI, sids_des_comptes,
)

log = logging.getLogger(__name__)

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
# Version des fichiers statiques, ajoutée à leur URL (?v=...) : après une mise à jour, les
# navigateurs rechargent le CSS et le JavaScript au lieu de garder l'ancienne version en cache
templates.env.globals["version_statique"] = max(int(f.stat().st_mtime) for f in (ICI / "static").iterdir())
def lien_dossier(source_id: int, chemin: str = "") -> str:
    """Accueil avec la recherche limitée à un dossier (et ses sous-dossiers)."""
    return "/?" + Filtres(dossier=f"{source_id}:{chemin}").lien()


templates.env.globals["lien_dossier"] = lien_dossier
templates.env.globals["fil_ariane"] = fil_ariane
templates.env.filters["date"] = lambda d: d.astimezone().strftime("%d/%m/%Y") if d else ""


MOIS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre")


def libelle_periode(filtres: Filtres) -> str:
    """« 2020 → 2022 », « mars 2021 », « depuis 2020 »... pour la pastille du filtre de dates."""
    def libelle(borne: str) -> str:
        annee, _, mois = borne.partition("-")
        return f"{MOIS[int(mois) - 1]} {annee}" if mois else annee

    debut, fin = filtres.periode()
    du, au = (libelle(filtres.du) if debut else ""), (libelle(filtres.au) if fin else "")
    if du and au:
        return du if du == au else f"{du} → {au}"
    return f"depuis {du}" if du else f"jusqu'à {au}" if au else ""


templates.env.globals["libelle_periode"] = libelle_periode


def lire_filtres(
    q: str = "",
    pole: str = "",
    famille: str = "",
    tag: Annotated[list[int], Query()] = [],
    dossier: str = "",
    du: str = "",
    au: str = "",
    vue: str = "",
    tri: str = "",
    page: int = 1,
) -> Filtres:
    """Filtres de recherche lus dans l'URL (communs à la page complète et au fragment de résultats)."""
    return Filtres(
        q=q, pole=pole, famille=famille, tags=tag, dossier=dossier, du=du, au=au,
        vue="liste" if vue == "liste" else "arbre",
        tri=tri if tri in ("recent", "ancien") else "",
        page=max(page, 1),
    )


def libelle_du_dossier(conn, filtres: Filtres) -> str | None:
    """« Source › dossier » du filtre dossier, pour l'afficher au-dessus des résultats."""
    cible = filtres.dossier_cible()
    if not cible:
        return None
    source = conn.execute("SELECT nom FROM source WHERE id = %s", (cible[0],)).fetchone()
    return " › ".join(filter(None, [source["nom"] if source else "?", *cible[1].split("\\")]))


def refus_authentification(request: Request, entete: str) -> HTMLResponse:
    """Réponse 401 : le navigateur s'identifie aussitôt avec la session Windows (sites intranet).
    La page ne s'affiche que s'il ne le fait pas."""
    return templates.TemplateResponse(
        request, "identification.html", {}, status_code=401, headers={"WWW-Authenticate": entete}
    )


def creer_app(cfg: configuration.Config | None = None, negociateur: Negociateur | None = None) -> FastAPI:
    """`negociateur` : vérification des jetons Windows (par défaut SSPI ; remplaçable dans les tests)."""
    cfg = cfg or configuration.charger()
    templates.env.globals["exemples_recherche"] = cfg.exemples_recherche
    pool = ConnectionPool(cfg.url_base, kwargs={"row_factory": dict_row}, min_size=1, max_size=10, open=False)

    @asynccontextmanager
    async def cycle_de_vie(app: FastAPI):
        pool.open()
        yield
        pool.close()

    app = FastAPI(title="GED DSI", lifespan=cycle_de_vie, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=ICI / "static"), name="static")

    # --- Qui est connecté ---------------------------------------------------------------
    # request.state.lecteur : la personne identifiée par sa session Windows (voir
    # ged.web.authentification) ; sans authentification, un lecteur qui voit tout.

    if cfg.authentification:
        auth = Authentification(
            cfg.protocole,
            negociateur or NegociateurSSPI(cfg.protocole, sids_des_comptes(cfg.groupes_admin)),
            cfg.duree_session_heures * 3600,
        )

        @app.middleware("http")
        async def authentifier(request: Request, suite):
            if request.url.path.startswith("/static/"):
                return await suite(request)
            lecteur = auth.lecteur_de_session(request.cookies.get(COOKIE))
            if lecteur:
                request.state.lecteur = lecteur
                return await suite(request)

            jeton = auth.jeton_recu(request.headers.get("authorization", ""))
            if jeton is None:
                # 127.0.0.1 est classé en zone Internet : le navigateur y demanderait un mot de
                # passe au lieu d'envoyer la session Windows, contrairement à localhost
                if request.url.hostname in ("127.0.0.1", "::1"):
                    return RedirectResponse(str(request.url.replace(hostname="localhost")), status_code=307)
                return refus_authentification(request, auth.entete_reponse())
            client = request.client
            # NTLM : les deux allers-retours de la négociation passent par la même connexion
            connexion = (client.host, client.port) if client else ("?", 0)
            try:
                sortant, lecteur = auth.negociateur.avancer(connexion, jeton)
            except AuthentificationRefusee as exc:
                log.warning("Identification refusée depuis %s : %s", connexion[0], exc)
                return refus_authentification(request, auth.entete_reponse())
            if lecteur is None:
                return refus_authentification(request, auth.entete_reponse(sortant))

            log.info("Connexion de %s%s", lecteur.compte, " (administrateur de la GED)" if lecteur.admin else "")
            request.state.lecteur = lecteur
            reponse = await suite(request)
            if sortant:  # Kerberos : jeton d'authentification mutuelle
                reponse.headers["WWW-Authenticate"] = auth.entete_reponse(sortant)
            reponse.set_cookie(COOKIE, auth.ouvrir_session(lecteur), httponly=True, samesite="lax")
            return reponse
    else:
        @app.middleware("http")
        async def sans_authentification(request: Request, suite):
            request.state.lecteur = Lecteur.sans_filtre()
            return await suite(request)

    def acces(request: Request, conn) -> FiltreAcces:
        return filtre_acces(conn, request.state.lecteur)

    def contexte_resultats(filtres: Filtres, conn, acces: FiltreAcces) -> dict:
        contexte = {
            "tags_actifs": conn.execute(
                "SELECT id, categorie, libelle FROM tag WHERE id = ANY(%s) ORDER BY categorie, libelle",
                (filtres.tags,),
            ).fetchall() if filtres.tags else [],
            "libelle_dossier": libelle_du_dossier(conn, filtres),
        }
        return {**contexte, **contexte_vue(filtres, conn, acces)}

    def contexte_vue(filtres: Filtres, conn, acces: FiltreAcces) -> dict:
        if filtres.vue == "arbre":
            resultats = rechercher(conn, filtres, acces, tous=True)
            return {
                "filtres": filtres,
                "resultats": resultats,
                "arbre": construire_arbre_resultats(resultats.documents),
                "tronque": resultats.total >= DOCUMENTS_ARBRE_MAX,
                "gabarit": "_arbre.html",
            }
        resultats = rechercher(conn, filtres, acces)
        return {
            "filtres": filtres,
            "resultats": resultats,
            "nb_pages": max(1, -(-resultats.total // PAR_PAGE)),
            "gabarit": "_resultats.html",
        }

    @app.get("/", response_class=HTMLResponse)
    def accueil(request: Request, filtres: Annotated[Filtres, Depends(lire_filtres)]):
        with pool.connection() as conn:
            filtre = acces(request, conn)
            contexte = contexte_resultats(filtres, conn, filtre)
            choix = facettes(conn, filtre)
        tags_par_categorie: dict[str, list[dict]] = {}
        for t in choix["tags"]:
            tags_par_categorie.setdefault(t["categorie"], []).append(t)
        return templates.TemplateResponse(
            request,
            "recherche.html",
            {**contexte, "choix": choix, "tags_par_categorie": tags_par_categorie},
        )

    @app.get("/resultats", response_class=HTMLResponse)
    def resultats_seuls(request: Request, filtres: Annotated[Filtres, Depends(lire_filtres)]):
        """Fragment HTML de la liste de résultats, pour la recherche pendant la frappe."""
        with pool.connection() as conn:
            contexte = contexte_resultats(filtres, conn, acces(request, conn))
        return templates.TemplateResponse(request, contexte["gabarit"], contexte)

    @app.get("/suggestions")
    def suggestions(request: Request, q: str = ""):
        """Suggestions de la liste déroulante sous la barre de recherche (JSON)."""
        with pool.connection() as conn:
            return suggerer(conn, q[:200], acces(request, conn))

    @app.get("/document/{document_id}", response_class=HTMLResponse)
    def detail(request: Request, document_id: int):
        with pool.connection() as conn:
            filtre = acces(request, conn)
            # Document hors des droits de la personne : même réponse que s'il n'existait pas
            doc = conn.execute(
                "SELECT d.*, s.nom AS source, s.pole FROM document d JOIN source s ON s.id = d.source_id "
                f"WHERE d.id = %(id)s AND {filtre.sql}",
                {"id": document_id, **filtre.parametres},
            ).fetchone()
            if not doc:
                raise HTTPException(404, "Document inconnu")
            tags = conn.execute(
                "SELECT t.id, t.categorie, t.libelle, dt.origine FROM document_tag dt JOIN tag t ON t.id = dt.tag_id "
                "WHERE dt.document_id = %s ORDER BY t.categorie, t.libelle",
                (document_id,),
            ).fetchall()
            doublons = conn.execute(
                "SELECT d.id, d.chemin_relatif FROM document d "
                f"WHERE d.empreinte = %(empreinte)s AND d.id <> %(id)s AND d.supprime_le IS NULL AND {filtre.sql} "
                "ORDER BY d.chemin_relatif",
                {"empreinte": doc["empreinte"], "id": document_id, **filtre.parametres},
            ).fetchall() if doc["empreinte"] else []
        dossier = dossier_parent(doc["chemin_relatif"])
        return templates.TemplateResponse(
            request,
            "document.html",
            {"doc": doc, "tags": tags, "doublons": doublons, "dossier": dossier, "ariane": fil_ariane(dossier)},
        )

    @app.get("/dossiers")
    def dossiers(source: int | None = None, chemin: str = ""):
        """Ancienne vue explorateur : remplacée par l'arborescence de l'accueil limitée au dossier."""
        return RedirectResponse(lien_dossier(source, chemin) if source is not None else "/", status_code=301)

    @app.get("/document/{document_id}/fichier")
    def fichier(request: Request, document_id: int):
        """Sert le fichier d'origine. Seuls les chemins présents dans l'index, et que la personne
        a le droit de lire, sont accessibles (le serveur lit le fichier avec son propre compte)."""
        with pool.connection() as conn:
            filtre = acces(request, conn)
            doc = conn.execute(
                "SELECT d.chemin, d.nom, d.extension FROM document d "
                f"WHERE d.id = %(id)s AND d.supprime_le IS NULL AND {filtre.sql}",
                {"id": document_id, **filtre.parametres},
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
        # Chemins, erreurs et volumes de toutes les sources : réservé aux administrateurs de la GED
        if not request.state.lecteur.admin:
            raise HTTPException(403, "Page réservée aux administrateurs de la GED")
        with pool.connection() as conn:
            donnees = statistiques(conn)
        return templates.TemplateResponse(request, "stats.html", donnees)

    return app
