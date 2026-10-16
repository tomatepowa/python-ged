"""Assistant d'installation : connexion à la base, puis tous les réglages de la GED.

Lancé par « ged serveur » tant que la GED n'est pas installée (ou par « ged serveur --assistant »
pour corriger des réglages, par exemple des administrateurs mal saisis). Il ne répond qu'aux
connexions de ce poste (127.0.0.1) et demande le code à usage unique affiché dans la console :
personne d'autre ne peut configurer la GED à votre place.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs

import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from starlette.background import BackgroundTask

from ged import parametres
from ged.config import PORT_DEFAUT, Connexion, ecrire_connexion, lire_connexion
from ged.db import initialiser
from ged.web.app import verifier_provenance

ICI = Path(__file__).parent
templates = Jinja2Templates(directory=ICI / "templates")
COOKIE = "ged_assistant"
ESSAIS_MAX = 10
LOCALES = {"127.0.0.1", "::1"}
# Sans ambiguïté à la lecture : ni 0/O, ni 1/I/L
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def generer_code() -> str:
    """Code à usage unique, affiché dans la console : « ABCD-EFGH »."""
    lettres = "".join(secrets.choice(ALPHABET) for _ in range(8))
    return f"{lettres[:4]}-{lettres[4:]}"


def champs_du_formulaire(corps: bytes) -> dict[str, list[str]]:
    """Champs d'un formulaire, valeurs vides comprises (les lignes de sources restent alignées)."""
    return parse_qs(corps.decode("utf-8"), keep_blank_values=True)


def creer_app_assistant(fichier: Path, terminer: Callable[[], None], code: str | None = None,
                        taxonomie: Path = Path("taxonomie.toml")) -> FastAPI:
    """`terminer` : appelé une fois l'installation enregistrée (arrête l'assistant, le serveur de la
    GED prend le relais). `code` : imposé dans les tests."""
    etat = {
        "code": code or generer_code(), "essais": 0, "jeton": None, "termine": False,
        # Connexion existante (assistant relancé) : la base est préremplie, le mot de passe gardé
        "connexion": lire_connexion(fichier), "url": None,
    }
    app = FastAPI(title="GED DSI : installation", docs_url=None, redoc_url=None)
    app.state.assistant = etat
    app.mount("/static", StaticFiles(directory=ICI / "static"), name="static")

    @app.middleware("http")
    async def ce_poste_seulement(request: Request, suite):
        if not request.client or request.client.host not in LOCALES:
            return HTMLResponse("L'assistant d'installation ne répond qu'aux connexions depuis le serveur lui-même.",
                                status_code=403)
        return await suite(request)

    def identifie(request: Request) -> bool:
        return bool(etat["jeton"]) and secrets.compare_digest(request.cookies.get(COOKIE, ""), etat["jeton"])

    def page_connexion(request: Request, erreur: str = "", saisie: dict | None = None, statut: int = 200):
        actuelle = conninfo_to_dict(etat["connexion"].url) if etat["connexion"] else {}
        saisie = saisie or {
            "hote_base": actuelle.get("host", "localhost"), "port_base": actuelle.get("port", "5432"),
            "base": actuelle.get("dbname", "ged"), "utilisateur": actuelle.get("user", "ged"),
        }
        return templates.TemplateResponse(request, "assistant_connexion.html", {
            "erreur": erreur, "saisie": saisie, "mot_de_passe_connu": bool(actuelle.get("password")),
            "bloque": etat["essais"] >= ESSAIS_MAX,
        }, status_code=statut)

    @app.get("/assistant", response_class=HTMLResponse)
    def accueil(request: Request):
        if etat["termine"]:
            return HTMLResponse("Installation terminée : la GED démarre.")
        if identifie(request) and etat["url"]:
            return RedirectResponse("/assistant/reglages", status_code=303)
        return page_connexion(request)

    @app.post("/assistant/connexion", response_class=HTMLResponse)
    async def connexion(request: Request):
        verifier_provenance(request)
        champs = {cle: valeurs[0].strip() for cle, valeurs in champs_du_formulaire(await request.body()).items()}
        saisie = {cle: champs.get(cle, "") for cle in ("hote_base", "port_base", "base", "utilisateur")}
        if etat["essais"] >= ESSAIS_MAX:
            return page_connexion(request, "Trop d'essais : relancez « ged serveur » pour obtenir un nouveau code.",
                                  saisie, 403)
        if not secrets.compare_digest(champs.get("code", "").upper().replace(" ", ""), etat["code"]):
            etat["essais"] += 1
            return page_connexion(request, "Code incorrect : recopiez celui affiché dans la console du serveur.",
                                  saisie, 403)
        mot_de_passe = champs.get("mot_de_passe", "")
        if not mot_de_passe and etat["connexion"]:
            mot_de_passe = conninfo_to_dict(etat["connexion"].url).get("password", "")
        url = make_conninfo(host=saisie["hote_base"], port=saisie["port_base"], dbname=saisie["base"],
                            user=saisie["utilisateur"], password=mot_de_passe or None)
        try:
            psycopg.connect(url, connect_timeout=5).close()
            initialiser(url)
        except (psycopg.Error, SystemExit) as exc:
            return page_connexion(request, f"Connexion impossible : {str(exc).strip()}", saisie, 400)
        etat["url"] = url
        etat["jeton"] = secrets.token_urlsafe(24)
        reponse = RedirectResponse("/assistant/reglages", status_code=303)
        reponse.set_cookie(COOKIE, etat["jeton"], httponly=True, samesite="strict")
        return reponse

    def page_reglages(request: Request, formulaire: parametres.Formulaire | None = None, statut: int = 200):
        with psycopg.connect(etat["url"], row_factory=dict_row) as conn:
            deja = parametres.est_installee(conn)
            valeurs = parametres.lire(conn) if deja else parametres.detections()
            taxonomie_vide = not conn.execute("SELECT 1 FROM reglage WHERE cle = 'taxonomie_importee_le'").fetchone()
        connexion = etat["connexion"]
        return templates.TemplateResponse(request, "assistant_reglages.html", {
            "v": formulaire.valeurs if formulaire else valeurs,
            "hote": formulaire.hote if formulaire else (connexion.hote if connexion else "127.0.0.1"),
            "port": formulaire.port if formulaire else (connexion.port if connexion else PORT_DEFAUT),
            "erreurs": formulaire.erreurs if formulaire else [], "assistant": True, "deja": deja,
            "nb_prepares": len(valeurs["exclure_prepares"]),
            "proposer_taxonomie": taxonomie_vide and taxonomie.is_file(), "taxonomie": taxonomie,
        }, status_code=statut)

    @app.get("/assistant/reglages", response_class=HTMLResponse)
    def reglages(request: Request):
        if not (identifie(request) and etat["url"]):
            return RedirectResponse("/assistant", status_code=303)
        return page_reglages(request)

    @app.post("/assistant/reglages", response_class=HTMLResponse)
    async def enregistrer(request: Request):
        verifier_provenance(request)
        if not (identifie(request) and etat["url"]):
            raise HTTPException(403, "Session de l'assistant expirée : recommencez depuis le début.")
        champs = champs_du_formulaire(await request.body())
        with psycopg.connect(etat["url"], row_factory=dict_row) as conn:
            formulaire = parametres.lire_formulaire(champs, parametres.lire(conn))
            if formulaire.erreurs:
                return page_reglages(request, formulaire, 400)
            parametres.ecrire(conn, formulaire.valeurs, "assistant d'installation", installation=True)
            if champs.get("importer_taxonomie") == ["1"] and taxonomie.is_file():
                from ged.taxonomie_base import importer_au_premier_demarrage

                importer_au_premier_demarrage(conn, taxonomie)
                conn.commit()
        ecrire_connexion(Connexion(etat["url"], formulaire.hote, formulaire.port), fichier)
        etat["termine"] = True
        # Page affichée par l'assistant lui-même, qui s'arrête une fois la réponse partie : le
        # serveur de la GED démarre ensuite, la page le guette puis y emmène
        return templates.TemplateResponse(request, "assistant_termine.html", {
            "avertissements": formulaire.avertissements, "port": formulaire.port, "fichier": fichier.resolve(),
        }, background=BackgroundTask(terminer))

    @app.get("/{chemin:path}", include_in_schema=False)
    def ailleurs(chemin: str):
        return RedirectResponse("/assistant", status_code=303)

    return app
