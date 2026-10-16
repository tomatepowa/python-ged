"""Application web de recherche.

Les fichiers des partages sont seulement lus. Seuls les tags posés à la main, leur gestion et la
taxonomie (mots-clés des tags) par les administrateurs modifient la base depuis l'interface
(voir ged.tags_manuels et ged.taxonomie_base). Le dépôt de documents (ged.depot) écrit dans la
zone d'attente, puis, après validation, dans la seule source du dépôt.
"""

from __future__ import annotations

import logging
import mimetypes
import uuid
from html import escape
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from ged import config as configuration
from ged import depot, documents, parametres, tags_manuels, taxonomie_base
from ged.commandes import aide as aide_des_commandes
from ged.acces import FiltreAcces, Lecteur, diagnostic_acces, filtre_acces, sids_accordant
from ged.annuaire import AnnuaireAD, nom_complet_local, organiser_groupes, prefixe_machine
from ged.arborescence import construire_arbre_resultats, dossier_parent, fil_ariane
from ged.droits import nom_compte
from ged.recherche import (
    DOCUMENTS_ARBRE_MAX, PAR_PAGE, ROLES, Filtres, chercher_personnes, facettes, mes_documents, rechercher,
    statistiques, suggerer,
)
from ged.recalcul_tags import RecalculEnArrierePlan
from ged.web.authentification import (
    COOKIE, COOKIE_NAVIGATEUR, Authentification, AuthentificationRefusee, Negociateur, NegociateurSSPI,
    identification_sur_demande, sids_des_comptes,
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


def lien_personne(noms: str | list[str], role: str = "") -> str:
    """Accueil avec les documents d'une personne (auteur, dernier modificateur, propriétaire),
    les plus récents d'abord."""
    noms = [noms] if isinstance(noms, str) else [n for n in noms if n]
    return "/?" + Filtres(personnes=noms, role=role, vue="liste", tri="recent").lien()


templates.env.globals["lien_personne"] = lien_personne
templates.env.globals["roles"] = ROLES
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
    personne: Annotated[list[str], Query()] = [],
    role: str = "",
    page: int = 1,
) -> Filtres:
    """Filtres de recherche lus dans l'URL (communs à la page complète et au fragment de résultats)."""
    return Filtres(
        q=q, pole=pole, famille=famille, tags=tag, dossier=dossier, du=du, au=au,
        vue="liste" if vue == "liste" else "arbre",
        tri=tri if tri in ("recent", "ancien") else "",
        personnes=[nom.strip()[:200] for nom in personne if nom.strip()][:5],
        role=role if role in ROLES else "",
        page=max(page, 1),
    )


def libelle_du_dossier(conn, filtres: Filtres) -> str | None:
    """« Source › dossier » du filtre dossier, pour l'afficher au-dessus des résultats."""
    cible = filtres.dossier_cible()
    if not cible:
        return None
    return " › ".join(filter(None, [documents.nom_source(conn, cible[0]) or "?", *cible[1].split("\\")]))


async def formulaire(request: Request, admin: bool = False) -> dict[str, str]:
    """Champs d'un formulaire qui modifie les tags (`admin` : réservé aux administrateurs).

    Refusé si la requête vient d'une autre page que la GED : un site malveillant ouvert dans le
    navigateur d'une personne ne peut pas poser ou retirer des tags à sa place.
    """
    if admin and not request.state.lecteur.admin:
        raise HTTPException(403, "Réservé aux administrateurs de la GED")
    verifier_provenance(request)
    champs = parse_qs((await request.body()).decode("utf-8"))
    return {cle: valeurs[0].strip() for cle, valeurs in champs.items()}


def verifier_provenance(request: Request) -> None:
    """Refuse une requête qui modifie quelque chose si elle vient d'une autre page que la GED."""
    provenance = request.headers.get("origin") or request.headers.get("referer") or ""
    if urlsplit(provenance).netloc != request.url.netloc:
        raise HTTPException(403, "Formulaire envoyé depuis une autre page que la GED")


def liste_entiers(corps: str, cle: str) -> list[int]:
    """Valeurs entières d'un champ répété (cases à cocher) d'un formulaire."""
    return [int(v) for v in parse_qs(corps).get(cle, []) if v.isdigit()]


def entier(champs: dict[str, str], cle: str) -> int | None:
    valeur = champs.get(cle, "")
    return int(valeur) if valeur.isdigit() else None


def avec_message(chemin: str, cle: str, texte: str | None) -> str:
    """URL de retour après un formulaire, avec le message à afficher."""
    if not texte:
        return chemin
    chemin, _, ancre = chemin.partition("#")
    return chemin + ("&" if "?" in chemin else "?") + urlencode({cle: texte}) + (f"#{ancre}" if ancre else "")


def refus_authentification(request: Request, entete: str | None) -> HTMLResponse:
    """Réponse 401 avec la page d'accueil de l'identification.

    Avec `entete` (WWW-Authenticate), le navigateur s'identifie aussitôt avec la session Windows
    (sites intranet) et la page ne s'affiche que s'il ne le fait pas. Sans, la page s'affiche
    d'abord : elle explique le réglage à faire avant que le navigateur ne demande un mot de passe.
    """
    suite = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return templates.TemplateResponse(
        request,
        "identification.html",
        {
            "firefox": identification_sur_demande(request.headers.get("user-agent", "")),
            "lien_connexion": "/connexion?" + urlencode({"suite": suite if request.url.path != "/connexion" else "/"}),
        },
        status_code=401,
        headers={"WWW-Authenticate": entete} if entete else None,
    )


class ConfigVivante:
    """La configuration du serveur, remplacée quand un administrateur enregistre la page Réglages :
    les requêtes suivantes voient les nouveaux réglages sans redémarrer."""

    def __init__(self, cfg: configuration.Config):
        object.__setattr__(self, "_cfg", cfg)

    def __getattr__(self, nom: str):
        return getattr(self._cfg, nom)

    def remplacer(self, cfg: configuration.Config) -> None:
        object.__setattr__(self, "_cfg", cfg)


def creer_app(
    cfg: configuration.Config | None = None, negociateur: Negociateur | None = None, annuaire=None,
    fichier_connexion: Path | None = None,
) -> FastAPI:
    """`negociateur` : vérification des jetons Windows (par défaut SSPI) ; `annuaire` : groupes
    parents (par défaut l'Active Directory). Remplaçables dans les tests. `fichier_connexion` :
    connexion.toml, réécrit si l'adresse d'écoute change dans la page Réglages."""
    cfg = ConfigVivante(cfg or configuration.charger(fichier_connexion))
    annuaire = annuaire or AnnuaireAD()

    def vue_groupes(lecteur: Lecteur):
        """Groupes de la personne, en arbre quand l'annuaire répond."""
        disponible = bool(lecteur.sid) and annuaire.disponible
        # Tous ses groupes lus d'un coup (quelques recherches groupées, puis gardés en mémoire)
        graphe = annuaire.graphe(lecteur.sids_refus | {lecteur.sid}) if disponible else {}
        return organiser_groupes(
            lecteur, lambda sid: graphe.get(sid, []), nom_compte, disponible, prefixe_machine()
        )
    def globales_des_gabarits() -> None:
        templates.env.globals["exemples_recherche"] = cfg.exemples_recherche
        templates.env.globals["depot_actif"] = bool(cfg.sources_depot)

    globales_des_gabarits()
    pool =ConnectionPool(cfg.url_base, kwargs={"row_factory": dict_row}, min_size=1, max_size=10, open=False)

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
            if request.url.path.startswith("/static/") or request.url.path == "/favicon.ico":
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
                # Firefox demanderait un mot de passe faute de réglage : la page d'accueil l'explique
                # d'abord, et son bouton « Se connecter » (/connexion) lance l'identification. Une fois
                # qu'il a su s'identifier, un cookie permet de négocier directement les fois suivantes.
                if (request.url.path != "/connexion" and request.cookies.get(COOKIE_NAVIGATEUR) != "1"
                        and identification_sur_demande(request.headers.get("user-agent", ""))):
                    return refus_authentification(request, None)
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
            reponse.set_cookie(COOKIE_NAVIGATEUR, "1", max_age=365 * 24 * 3600, httponly=True, samesite="lax")
            return reponse
    else:
        @app.middleware("http")
        async def sans_authentification(request: Request, suite):
            request.state.lecteur = Lecteur.sans_filtre()
            return await suite(request)

    def acces(request: Request, conn) -> FiltreAcces:
        return filtre_acces(conn, request.state.lecteur)

    def contexte_resultats(filtres: Filtres, conn, acces: FiltreAcces, admin: bool = False) -> dict:
        contexte = {
            "tags_actifs": documents.tags_choisis(conn, filtres.tags),
            "libelle_dossier": libelle_du_dossier(conn, filtres),
        }
        cible = filtres.dossier_cible()
        if admin and cible:
            # Tags du dossier affiché, modifiables par les administrateurs
            contexte["regles_dossier"] = tags_manuels.regles_du_dossier(conn, *cible)
            contexte["choix_tags"] = tags_manuels.tous_les_tags(conn)
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
            contexte = contexte_resultats(filtres, conn, filtre, request.state.lecteur.admin)
            choix = facettes(conn, filtre)
            # Aucun document visible : dire pourquoi, plutôt qu'une recherche qui ne trouve jamais rien
            alerte_acces = None if any(p["nb"] for p in choix["poles"]) else diagnostic_acces(conn)
        tags_par_categorie: dict[str, list[dict]] = {}
        for t in choix["tags"]:
            tags_par_categorie.setdefault(t["categorie"], []).append(t)
        return templates.TemplateResponse(
            request,
            "recherche.html",
            {**contexte, "choix": choix, "tags_par_categorie": tags_par_categorie, "alerte_acces": alerte_acces},
        )

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        """Les navigateurs demandent /favicon.ico d'office, même si la page en déclare une autre."""
        return FileResponse(ICI / "static" / "favicon.svg", media_type="image/svg+xml")

    @app.get("/connexion")
    def connexion(suite: str = "/"):
        """Bouton « Se connecter » de la page d'accueil : une fois identifié, retour à la page demandée."""
        # Seulement une page de la GED (pas de redirection vers un autre site)
        if not suite.startswith("/") or suite.startswith("//") or "\\" in suite:
            suite = "/"
        return RedirectResponse(suite, status_code=303)

    @app.get("/resultats", response_class=HTMLResponse)
    def resultats_seuls(request: Request, filtres: Annotated[Filtres, Depends(lire_filtres)]):
        """Fragment HTML de la liste de résultats, pour la recherche pendant la frappe."""
        with pool.connection() as conn:
            contexte = contexte_resultats(filtres, conn, acces(request, conn), request.state.lecteur.admin)
        return templates.TemplateResponse(request, contexte["gabarit"], contexte)

    @app.get("/suggestions")
    def suggestions(request: Request, q: str = ""):
        """Suggestions de la liste déroulante sous la barre de recherche (JSON)."""
        with pool.connection() as conn:
            return suggerer(conn, q[:200], acces(request, conn))

    @app.get("/personnes")
    def personnes(request: Request, q: str = ""):
        """Personnes dont le nom contient les mots tapés, pour le champ « Personne » des filtres (JSON)."""
        with pool.connection() as conn:
            return chercher_personnes(conn, q[:100], acces(request, conn))

    @app.get("/document/{document_id}", response_class=HTMLResponse)
    def detail(request: Request, document_id: int, retrait: int | None = None):
        lecteur: Lecteur = request.state.lecteur
        with pool.connection() as conn:
            # Document hors des droits de la personne : même réponse que s'il n'existait pas
            contenu = documents.fiche(conn, document_id, acces(request, conn))
            if contenu:
                contenu["choix_tags"] = tags_manuels.tous_les_tags(conn)
                # Juste après un retrait : de quoi l'annuler
                ligne = tags_manuels.ligne_du_journal(conn, retrait) if retrait else None
                if ligne and ligne["document_id"] == document_id and tags_manuels.annulable(ligne, lecteur):
                    contenu["retrait_annulable"] = ligne
        if not contenu:
            raise HTTPException(404, "Document inconnu")
        doc = contenu["doc"]
        dossier = dossier_parent(doc["chemin_relatif"])
        for tag in contenu["tags"]:
            tag["retirable"] = tags_manuels.peut_retirer(lecteur, tag)
        # Ce qui donne à la personne le droit de voir ce document (vide pour un administrateur
        # de la GED que ses groupes n'autorisent pas, ou sans identification)
        sids = sids_accordant(doc["acces_sids"], doc["acces_ordre"], lecteur)
        # Pour un groupe hérité, la chaîne qui y mène : « GED-Lecture, via DSI-Infra › DSI »
        chemins = vue_groupes(lecteur).chemins if sids else {}
        accordant = [{"nom": nom_compte(sid), "via": chemins.get(sid, [])[:-1]} for sid in sids]
        mes_comptes = {nom_compte(sid) for sid in lecteur.sids_refus} if lecteur.compte else set()
        return templates.TemplateResponse(
            request,
            "document.html",
            {
                **contenu, "dossier": dossier, "ariane": fil_ariane(dossier),
                "accordant": accordant, "mes_comptes": mes_comptes,
            },
        )

    @app.get("/moi", response_class=HTMLResponse)
    def moi(request: Request):
        """Compte Windows de la personne connectée et ses groupes (ceux qui comptent pour ses droits)."""
        lecteur: Lecteur = request.state.lecteur
        documents, personnes, nom_affiche = None, [], ""
        if lecteur.compte:
            # Nom complet, identifiant seul (« acosson », certains postes signent ainsi les documents)
            # et compte Windows (propriétaire des fichiers)
            identifiant = lecteur.compte.split("\\")[-1].split("@")[0]
            # Nom complet lu par le serveur avec son propre compte (annuaire, ou base locale pour un
            # compte local) : l'identité de la personne obtenue par NTLM ne permet pas d'interroger
            # le contrôleur de domaine depuis le serveur (pas de « second saut »)
            compte = lecteur.sid and nom_compte(lecteur.sid)
            nom_affiche = (
                (lecteur.sid and annuaire.disponible and annuaire.nom_affiche(lecteur.sid))
                or (compte and nom_complet_local(compte))
                or ""
            )
            personnes = [nom for nom in (nom_affiche, identifiant, compte) if nom]
            with pool.connection() as conn:
                documents = mes_documents(conn, acces(request, conn), personnes)
        return templates.TemplateResponse(
            request,
            "moi.html",
            {
                "lecteur": lecteur, "groupes": vue_groupes(lecteur),
                "documents": documents, "personnes": personnes, "nom_affiche": nom_affiche,
            },
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
            doc = documents.fichier(conn, document_id, acces(request, conn))
        if not doc:
            raise HTTPException(404, "Document inconnu")
        chemin = Path(doc["chemin"])
        if not chemin.is_file():
            raise HTTPException(410, "Le fichier n'existe plus à cet emplacement (réindexation en attente)")
        type_mime = mimetypes.guess_type(doc["nom"])[0] or "application/octet-stream"
        disposition = "inline" if doc["extension"] in AFFICHABLES else "attachment"
        return FileResponse(chemin, media_type=type_mime, filename=doc["nom"], content_disposition_type=disposition)

    # --- Tags posés à la main -------------------------------------------------------------

    @app.post("/document/{document_id}/tags")
    async def tags_document(request: Request, document_id: int):
        """Pose un tag existant sur le document, ou en retire un (voir tags_manuels.peut_retirer)."""
        champs = await formulaire(request)
        lecteur: Lecteur = request.state.lecteur

        def appliquer() -> tuple[str | None, int | None]:
            """(erreur, ligne du journal du retrait, pour pouvoir l'annuler)"""
            with pool.connection() as conn:
                # Seulement sur un document que la personne a le droit de voir
                if not documents.visible(conn, document_id, acces(request, conn)):
                    raise HTTPException(404, "Document inconnu")
                try:
                    if (tag := entier(champs, "retirer")) is not None:
                        return None, tags_manuels.retirer(conn, document_id, tag, lecteur, taxonomie_base.charger(conn))
                    if (tag := entier(champs, "tag_id")) is not None:
                        tags_manuels.ajouter(conn, document_id, tag, lecteur)
                except (tags_manuels.TagInvalide, tags_manuels.RetraitRefuse) as exc:
                    return str(exc), None
            return None, None

        erreur, retrait = await run_in_threadpool(appliquer)
        chemin = f"/document/{document_id}" + (f"?retrait={retrait}" if retrait else "") + "#tags"
        return RedirectResponse(avec_message(chemin, "erreur_tag", erreur), status_code=303)

    @app.post("/journal/{journal_id}/annuler")
    async def annuler_retrait(request: Request, journal_id: int):
        """Annule un retrait de tag (document ou dossier) : retour à la page d'où vient la demande."""
        champs = await formulaire(request)
        lecteur: Lecteur = request.state.lecteur
        retour = champs.get("retour", "/")
        # Seulement une page de la GED (pas de redirection vers un autre site)
        if not retour.startswith("/") or retour.startswith("//") or "\\" in retour:
            retour = "/"
        cle_message, cle_erreur = ("message_tag", "erreur_tag") if retour.startswith("/document/") else ("message", "erreur")

        def appliquer() -> str:
            with pool.connection() as conn:
                ligne = tags_manuels.ligne_du_journal(conn, journal_id)
                # Un retrait sur un document que la personne ne voit plus ne s'annule pas depuis chez elle
                if ligne and ligne["document_id"] and not documents.visible(conn, ligne["document_id"], acces(request, conn)):
                    raise HTTPException(404, "Document inconnu")
                return tags_manuels.annuler(conn, journal_id, lecteur)

        try:
            message = await run_in_threadpool(appliquer)
        except (tags_manuels.TagInvalide, tags_manuels.RetraitRefuse) as exc:
            return RedirectResponse(avec_message(retour, cle_erreur, str(exc)), status_code=303)
        return RedirectResponse(avec_message(retour, cle_message, message), status_code=303)

    @app.post("/dossier/tags")
    async def tags_dossier(request: Request):
        """Pose un tag sur un dossier (tous ses documents, présents et futurs) ou le retire."""
        champs = await formulaire(request, admin=True)
        lecteur: Lecteur = request.state.lecteur
        filtres = Filtres(dossier=champs.get("dossier", ""))
        cible = filtres.dossier_cible()
        if not cible:
            raise HTTPException(400, "Dossier manquant")

        def appliquer() -> None:
            with pool.connection() as conn:
                if (regle := entier(champs, "retirer")) is not None:
                    tags_manuels.retirer_du_dossier(conn, regle, lecteur)
                elif (tag := entier(champs, "tag_id")) is not None:
                    tags_manuels.ajouter_au_dossier(conn, *cible, tag, lecteur)

        await run_in_threadpool(appliquer)
        return RedirectResponse("/?" + filtres.lien(), status_code=303)

    # --- Gestion des tags et de la taxonomie (administrateurs) --------------------------------

    # Recalculs lancés depuis la page Tags, avec leur propre connexion (ils peuvent durer)
    recalcul = RecalculEnArrierePlan(lambda: psycopg.connect(cfg.url_base, row_factory=dict_row))
    app.state.recalcul = recalcul

    def exiger_admin(request: Request) -> None:
        if not request.state.lecteur.admin:
            raise HTTPException(403, "Page réservée aux administrateurs de la GED")

    @app.get("/admin/tags", response_class=HTMLResponse)
    def admin_tags(request: Request, message: str = "", erreur: str = ""):
        exiger_admin(request)
        with pool.connection() as conn:
            tags = tags_manuels.tags_administres(conn)
            categories = tags_manuels.categories(conn)
            seuil, densite_min = taxonomie_base.reglages(conn)
        par_categorie: dict[str, list[dict]] = {}
        for tag in tags:
            par_categorie.setdefault(tag["categorie"], []).append(tag)
        return templates.TemplateResponse(request, "admin_tags.html", {
            "par_categorie": par_categorie, "tags": tags, "categories": categories,
            "seuil": seuil, "densite_min": densite_min, "recalcul": recalcul.etat(),
            "message": message, "erreur": erreur,
        })

    @app.post("/admin/tags")
    async def admin_tags_modifier(request: Request):
        """Crée, renomme, fusionne ou supprime un tag ; modifie ses mots-clés ou les réglages de la
        taxonomie (les tags se recalculent ensuite en arrière-plan)."""
        champs = await formulaire(request, admin=True)
        lecteur: Lecteur = request.state.lecteur
        action, tag = champs.get("action", ""), entier(champs, "tag_id")

        def appliquer() -> str:
            with pool.connection() as conn:
                if action == "creer":
                    tags_manuels.creer(conn, champs.get("categorie", ""), champs.get("libelle", ""), lecteur)
                    return "Tag créé : ajoutez-lui des mots-clés pour qu'il soit posé automatiquement."
                if action == "reglages":
                    try:
                        seuil = int(champs.get("seuil", ""))
                        densite = float(champs.get("densite_min", "").replace(",", "."))
                    except ValueError:
                        raise tags_manuels.TagInvalide("Seuil et densité : des nombres.") from None
                    if not taxonomie_base.modifier_reglages(conn, seuil, densite, lecteur):
                        return "Réglages inchangés."
                    conn.commit()
                    recalcul.demander(None, "tous les tags")
                    return "Réglages enregistrés : recalcul de tous les tags lancé."
                if action == "recalculer":
                    recalcul.demander(None, "tous les tags")
                    return "Recalcul de tous les tags lancé."
                ligne = conn.execute("SELECT libelle FROM tag WHERE id = %s", (tag,)).fetchone()
                if not ligne:
                    raise tags_manuels.TagInvalide("Ce tag n'existe plus.")
                if action == "mots_cles":
                    if not taxonomie_base.modifier_mots(conn, tag, champs.get("mots", "").splitlines(), lecteur):
                        return "Mots-clés inchangés."
                    conn.commit()
                    recalcul.demander(tag, f"« {ligne['libelle']} »")
                    return f"Mots-clés de « {ligne['libelle']} » enregistrés : recalcul du tag lancé."
                if action == "renommer":
                    tags_manuels.renommer(conn, tag, champs.get("categorie", ""), champs.get("libelle", ""), lecteur)
                    return "Tag renommé."
                if action == "fusionner":
                    cible = entier(champs, "cible") or 0
                    nb = tags_manuels.fusionner(conn, tag, cible, lecteur)
                    conn.commit()
                    # Les mots-clés du tag fusionné se sont ajoutés à ceux de la cible
                    libelle_cible = conn.execute("SELECT libelle FROM tag WHERE id = %s", (cible,)).fetchone()["libelle"]
                    recalcul.demander(cible, f"« {libelle_cible} »")
                    return f"Tags fusionnés ({nb} documents reportés)."
                if action == "supprimer":
                    nb = tags_manuels.supprimer(conn, tag, lecteur)
                    return f"Tag supprimé ({nb} documents)."
                raise tags_manuels.TagInvalide("Action inconnue.")

        try:
            message = await run_in_threadpool(appliquer)
        except tags_manuels.TagInvalide as exc:
            return RedirectResponse(avec_message("/admin/tags", "erreur", str(exc)), status_code=303)
        ancre = f"#tag-{tag}" if tag and action in ("mots_cles", "renommer") else ""
        return RedirectResponse(avec_message(f"/admin/tags{ancre}", "message", message), status_code=303)

    @app.post("/admin/tags/apercu", response_class=HTMLResponse)
    async def admin_tags_apercu(request: Request):
        """Fragment HTML : ce que donneraient ces mots-clés, sans rien enregistrer."""
        champs = await formulaire(request, admin=True)

        def calculer() -> dict:
            with pool.connection() as conn:
                return taxonomie_base.apercu(conn, entier(champs, "tag_id"), champs.get("mots", "").splitlines())

        try:
            resultat = await run_in_threadpool(calculer)
        except tags_manuels.TagInvalide as exc:
            return HTMLResponse(f'<p class="avertissement">{escape(str(exc))}</p>')
        return templates.TemplateResponse(request, "_apercu_tag.html", {"apercu": resultat})

    @app.get("/admin/tags/recalcul")
    def admin_tags_recalcul(request: Request):
        """Avancement du recalcul en arrière-plan (JSON, interrogé par la page Tags)."""
        exiger_admin(request)
        return recalcul.etat()

    @app.get("/admin/taxonomie.toml")
    def admin_taxonomie_export(request: Request):
        """Export de la taxonomie au format de taxonomie.toml (sauvegarde, versionnage)."""
        exiger_admin(request)
        with pool.connection() as conn:
            texte = taxonomie_base.exporter(conn)
        return Response(texte, media_type="application/toml; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="taxonomie.toml"'})

    @app.get("/admin/journal", response_class=HTMLResponse)
    def admin_journal(request: Request, par: str = "", tag: int | None = None, message: str = "", erreur: str = ""):
        exiger_admin(request)
        with pool.connection() as conn:
            lignes = tags_manuels.journal(conn, par, tag)
            for ligne in lignes:
                ligne["annulable"] = tags_manuels.annulable(ligne, request.state.lecteur)
            personnes = tags_manuels.personnes_du_journal(conn)
            libelle_tag = conn.execute(
                "SELECT libelle || ' (' || categorie || ')' AS nom FROM tag WHERE id = %s", (tag,)
            ).fetchone() if tag else None
        return templates.TemplateResponse(request, "admin_journal.html", {
            "lignes": lignes, "personnes": personnes, "par": par, "tag": tag,
            "libelle_tag": libelle_tag["nom"] if libelle_tag else None, "limite": tags_manuels.JOURNAL_MAX,
            "message": message, "erreur": erreur,
        })

    # --- Dépôt de nouveaux documents : déposer et classer dans le même geste (ged.depot) -------

    def exiger_depot() -> None:
        if not cfg.sources_depot:
            raise HTTPException(404, "Dépôt de documents désactivé ([depot] sources dans la configuration)")

    def contexte_classement(conn, depots: list[dict]) -> dict:
        """De quoi afficher le formulaire de classement de ces dépôts (gabarit _classement.html)."""
        tags = tags_manuels.tous_les_tags(conn)
        for d in depots:
            d["options"] = depot.options_emplacement(d)
            choix = d["choix"] or {}
            d["nom_choisi"] = choix.get("nom") or d["nom"]
            d["tags_choisis"] = choix["tags"] if "tags" in choix else d["tags_proposes"]
        return {
            "tags": tags, "libelles": {t["id"]: t for t in tags},
            "sources_depot": cfg.sources_du_depot(), "dossiers": depot.dossiers_des_sources(conn, cfg),
        }

    @app.get("/depot", response_class=HTMLResponse)
    def page_depot(request: Request, message: str = "", erreur: str = "", document: int | None = None):
        exiger_depot()
        lecteur: Lecteur = request.state.lecteur
        with pool.connection() as conn:
            a_classer = depot.a_classer_par(conn, lecteur)
            contexte = contexte_classement(conn, a_classer)
            mes_depots = depot.de_la_personne(conn, lecteur)
            nb_envoyes = depot.nb_envoyes(conn) if lecteur.admin else 0
        return templates.TemplateResponse(request, "depot.html", {
            **contexte, "a_classer": a_classer, "mes_depots": mes_depots, "nb_envoyes": nb_envoyes,
            "taille_max_mo": cfg.taille_max_depot_mo, "message": message, "erreur": erreur,
            "document_classe": document, "retour": "/depot",
        })

    @app.post("/depot")
    async def recevoir_depot(request: Request):
        """Reçoit un fichier envoyé par la page Déposer : le corps de la requête est le fichier,
        son nom et le commentaire sont dans les en-têtes (encodés comme une URL)."""
        exiger_depot()
        verifier_provenance(request)
        nom = unquote(request.headers.get("x-nom-fichier", ""))
        commentaire = unquote(request.headers.get("x-commentaire", ""))
        limite = cfg.taille_max_depot_mo * 1024 * 1024
        trop_gros = f"Fichier de plus de {cfg.taille_max_depot_mo} Mo."
        if int(request.headers.get("content-length") or 0) > limite:
            return Response(trop_gros, status_code=413)
        cfg.attente_depot.mkdir(parents=True, exist_ok=True)
        recu = cfg.attente_depot / f"reception-{uuid.uuid4().hex}.part"
        try:
            taille = 0
            with recu.open("wb") as f:
                async for bloc in request.stream():
                    taille += len(bloc)
                    if taille > limite:
                        raise depot.DepotRefuse(trop_gros)
                    f.write(bloc)

            def enregistrer() -> int:
                with pool.connection() as conn:
                    return depot.enregistrer(conn, cfg, recu, nom, request.state.lecteur, commentaire)

            depot_id = await run_in_threadpool(enregistrer)
        except depot.DepotRefuse as exc:
            return Response(str(exc), status_code=400)
        finally:
            # Renommé <id><extension> une fois enregistré : il ne reste que si la réception a échoué
            recu.unlink(missing_ok=True)
        log.info("Dépôt %d : %s par %s", depot_id, nom, request.state.lecteur.compte or "?")
        return {"id": depot_id}

    @app.get("/depot/{depot_id}/fichier")
    def fichier_depose(request: Request, depot_id: int):
        """Fichier pas encore classé : pour les administrateurs et la personne qui l'a déposé."""
        exiger_depot()
        lecteur: Lecteur = request.state.lecteur
        with pool.connection() as conn:
            ligne = depot.depot(conn, depot_id)
        if not ligne or not (lecteur.admin or depot.est_a_lui(ligne, lecteur)):
            raise HTTPException(404, "Dépôt inconnu")
        chemin = depot.fichier_en_attente(cfg, ligne)
        if ligne["statut"] != "attente" or not chemin.is_file():
            raise HTTPException(410, "Ce dépôt n'est plus en attente")
        type_mime = mimetypes.guess_type(ligne["nom"])[0] or "application/octet-stream"
        disposition = "inline" if ligne["extension"] in AFFICHABLES else "attachment"
        return FileResponse(chemin, media_type=type_mime, filename=ligne["nom"], content_disposition_type=disposition)

    def page_de_retour(champs: dict[str, str]) -> str:
        retour = champs.get("retour", "/depot")
        return retour if retour in ("/depot", "/admin/depots") else "/depot"

    @app.post("/depot/{depot_id}/classer")
    async def classer_depot(request: Request, depot_id: int):
        """Classe le dépôt (administrateur) ou envoie ce classement en validation (autre personne)."""
        exiger_depot()
        champs = await formulaire(request)
        corps = (await request.body()).decode("utf-8")
        lecteur: Lecteur = request.state.lecteur
        retour = page_de_retour(champs)

        def appliquer() -> int | None:
            with pool.connection() as conn:
                ligne = depot.depot(conn, depot_id)
                if not ligne or not (lecteur.admin or depot.est_a_lui(ligne, lecteur)):
                    raise HTTPException(404, "Dépôt inconnu")
                # Un emplacement proposé (par son rang), ou un autre choisi à la main
                options = depot.options_emplacement(ligne)
                rang = entier(champs, "emplacement")
                if rang is not None and rang < len(options):
                    source, dossier = options[rang]["source"], options[rang]["dossier"]
                else:
                    source, dossier = champs.get("source", ""), champs.get("dossier", "")
                return depot.classer(conn, cfg, depot_id, lecteur, champs.get("nom", ""), source, dossier,
                                     liste_entiers(corps, "tag"))

        try:
            document_id = await run_in_threadpool(appliquer)
        except (depot.DepotRefuse, tags_manuels.TagInvalide) as exc:
            return RedirectResponse(avec_message(f"{retour}#depot-{depot_id}", "erreur", str(exc)), status_code=303)
        except OSError as exc:
            log.exception("Classement du dépôt %d impossible", depot_id)
            return RedirectResponse(avec_message(f"{retour}#depot-{depot_id}", "erreur", f"Copie impossible : {exc}"),
                                    status_code=303)
        if document_id is None:
            return RedirectResponse(avec_message(retour, "message", "Envoyé : un administrateur de la GED va le valider."),
                                    status_code=303)
        return RedirectResponse(avec_message(f"{retour}?document={document_id}", "message", "Document classé."),
                                status_code=303)

    @app.post("/depot/{depot_id}/retirer")
    async def retirer_depot(request: Request, depot_id: int):
        """La personne qui a déposé retire son dépôt, ou un administrateur le rejette (motif facultatif)."""
        exiger_depot()
        champs = await formulaire(request)
        retour = page_de_retour(champs)
        lecteur: Lecteur = request.state.lecteur

        def appliquer() -> None:
            with pool.connection() as conn:
                ligne = depot.depot(conn, depot_id)
                a_lui = bool(ligne) and depot.est_a_lui(ligne, lecteur)
                motif = "Retiré par la personne qui l'a déposé" if a_lui else champs.get("motif", "")
                depot.rejeter(conn, cfg, depot_id, lecteur, motif)

        try:
            await run_in_threadpool(appliquer)
        except depot.DepotRefuse as exc:
            return RedirectResponse(avec_message(retour, "erreur", str(exc)), status_code=303)
        return RedirectResponse(avec_message(retour, "message", "Dépôt retiré."), status_code=303)

    @app.get("/admin/depots", response_class=HTMLResponse)
    def admin_depots(request: Request, message: str = "", erreur: str = "", document: int | None = None):
        """Dépôts envoyés en validation par des personnes qui ne sont pas administratrices."""
        exiger_depot()
        exiger_admin(request)
        with pool.connection() as conn:
            a_valider = depot.envoyes(conn)
            contexte = contexte_classement(conn, a_valider)
            historique = depot.traites(conn)
        return templates.TemplateResponse(request, "admin_depots.html", {
            **contexte, "a_valider": a_valider, "historique": historique,
            "message": message, "erreur": erreur, "document_classe": document, "retour": "/admin/depots",
        })

    # --- Réglages (administrateurs) ----------------------------------------------------------------

    def page_reglages(request: Request, formulaire: parametres.Formulaire | None = None, message: str = "",
                      statut: int = 200):
        with pool.connection() as conn:
            valeurs = parametres.lire(conn)
        return templates.TemplateResponse(request, "admin_reglages.html", {
            "v": formulaire.valeurs if formulaire else valeurs,
            "hote": formulaire.hote if formulaire else cfg.hote,
            "port": formulaire.port if formulaire else cfg.port,
            "erreurs": formulaire.erreurs if formulaire else [], "assistant": False,
            "nb_prepares": len(valeurs["exclure_prepares"]), "message": message,
            "avertissements": request.query_params.getlist("avertissement"),
        }, status_code=statut)

    @app.get("/admin/reglages", response_class=HTMLResponse)
    def admin_reglages(request: Request, message: str = ""):
        exiger_admin(request)
        return page_reglages(request, message=message)

    @app.post("/admin/reglages", response_class=HTMLResponse)
    async def admin_reglages_enregistrer(request: Request):
        exiger_admin(request)
        verifier_provenance(request)
        champs = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        lecteur: Lecteur = request.state.lecteur

        def enregistrer() -> parametres.Formulaire | list[str]:
            with pool.connection() as conn:
                formulaire = parametres.lire_formulaire(champs, parametres.lire(conn))
                if formulaire.erreurs:
                    return formulaire
                modifies = parametres.ecrire(conn, formulaire.valeurs, tags_manuels.auteur(lecteur))
                valeurs = parametres.lire(conn)
            # Pris en compte tout de suite, sauf ce qui n'est lu qu'au démarrage
            connexion = configuration.Connexion(cfg.url_base, cfg.hote, cfg.port)
            cfg.remplacer(parametres.config(connexion, valeurs))
            globales_des_gabarits()
            au_demarrage = [parametres.AU_DEMARRAGE[cle] for cle in modifies if cle in parametres.AU_DEMARRAGE]
            if (formulaire.hote, formulaire.port) != (cfg.hote, cfg.port):
                configuration.ecrire_connexion(
                    configuration.Connexion(cfg.url_base, formulaire.hote, formulaire.port), fichier_connexion)
                au_demarrage.append("adresse d'écoute")
            return [*formulaire.avertissements, *(
                [f"Pris en compte au prochain démarrage du serveur : {', '.join(au_demarrage)}."] if au_demarrage else []
            )]

        resultat = await run_in_threadpool(enregistrer)
        if isinstance(resultat, parametres.Formulaire):
            return page_reglages(request, resultat, statut=400)
        return RedirectResponse(
            "/admin/reglages?" + urlencode([("message", "Réglages enregistrés."), *(("avertissement", a) for a in resultat)]),
            status_code=303,
        )

    @app.get("/admin/commandes", response_class=HTMLResponse)
    def admin_commandes(request: Request):
        """Aide des commandes à lancer sur le serveur (même définition que la ligne de commande)."""
        exiger_admin(request)
        return templates.TemplateResponse(request, "admin_commandes.html", {"aide": aide_des_commandes()})

    @app.get("/stats", response_class=HTMLResponse)
    def stats(request: Request):
        # Chemins, erreurs et volumes de toutes les sources : réservé aux administrateurs de la GED
        if not request.state.lecteur.admin:
            raise HTTPException(403, "Page réservée aux administrateurs de la GED")
        with pool.connection() as conn:
            donnees = statistiques(conn)
        return templates.TemplateResponse(request, "stats.html", donnees)

    return app
