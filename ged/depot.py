"""Dépôt de nouveaux documents depuis l'interface web : déposer et classer dans le même geste.

1. Une personne dépose un fichier : il attend dans la zone d'attente ([depot] attente), hors des
   sources indexées, sous le nom <id><extension>.
2. La GED l'analyse aussitôt : texte (OCR compris pour un scan), tags proposés par la taxonomie,
   emplacements proposés (dossiers des documents les plus semblables, puis de ceux qui portent
   les mêmes tags), copies identiques déjà indexées.
3. Sur la même page, la personne choisit le nom, l'emplacement et les tags, puis classe :
   - un administrateur de la GED : le document est classé aussitôt ;
   - une autre personne : son choix part en validation (page À classer des administrateurs).
4. Au classement, la GED copie le fichier dans le dossier choisi, dans l'une des sources ouvertes
   au dépôt ([depot] sources, les seules où elle écrit), puis l'indexe tout de suite. Elle
   n'écrase ni ne modifie jamais un fichier existant.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING

import psycopg
from psycopg.types.json import Jsonb

from ged import tags_manuels, taxonomie_base
from ged.arborescence import dossier_parent
from ged.extraction import Resultat, extraire
from ged.indexeur import EXTENSIONS_BRUIT, EXTENSIONS_SENSIBLES, EXTENSIONS_TECHNIQUES
from ged.ocr import ocr

if TYPE_CHECKING:
    from ged.acces import Lecteur
    from ged.config import Config, Source

log = logging.getLogger(__name__)

NOM_MAX = 150
COMMENTAIRE_MAX = 1000
# Caractères interdits par Windows dans un nom de fichier ou de dossier
CARACTERES_INTERDITS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
NOMS_RESERVES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
# Emplacements proposés, et documents cités en exemple pour chacun
EMPLACEMENTS_MAX = 5
EXEMPLES_MAX = 3
# Documents semblables : mots les plus fréquents du texte déposé, documents comparés
MOTS_SEMBLABLES = 25
DOCUMENTS_SEMBLABLES = 30


class DepotRefuse(ValueError):
    pass


# --- Noms de fichiers et de dossiers ----------------------------------------------------------


def nom_valide(nom: str, quoi: str = "Nom de fichier") -> str:
    """Nom de fichier ou de dossier acceptable sous Windows, ou DepotRefuse."""
    nom = nom.strip().rstrip(". ")
    if not nom:
        raise DepotRefuse(f"{quoi} vide.")
    if len(nom) > NOM_MAX:
        raise DepotRefuse(f"{quoi} trop long ({NOM_MAX} caractères au plus).")
    if CARACTERES_INTERDITS.search(nom):
        raise DepotRefuse(f'{quoi} : caractères interdits (< > : " / \\ | ? *).')
    if nom in (".", "..") or nom.split(".")[0].lower() in NOMS_RESERVES:
        raise DepotRefuse(f"{quoi} réservé par Windows : {nom}")
    return nom


def dossier_valide(dossier: str) -> str:
    """Dossier relatif à une source ('' : sa racine), séparé par « \\ »."""
    parties = [p for p in re.split(r"[\\/]+", dossier.strip()) if p.strip()]
    return "\\".join(nom_valide(p, "Nom de dossier") for p in parties)


def nom_depose(nom: str) -> str:
    """Nom d'origine du fichier envoyé par le navigateur (sans le chemin du poste)."""
    return nom_valide(PureWindowsPath(nom.replace("/", "\\")).name)


def extension_acceptee(nom: str) -> str:
    extension = PureWindowsPath(nom).suffix.lower()
    if not extension:
        raise DepotRefuse("Fichier sans extension : impossible de savoir ce qu'il contient.")
    if extension in EXTENSIONS_SENSIBLES:
        raise DepotRefuse("Coffres de mots de passe, clés et certificats : à garder dans KeePass, pas dans la GED.")
    if extension in EXTENSIONS_TECHNIQUES | EXTENSIONS_BRUIT:
        raise DepotRefuse(f"Les fichiers {extension} ne sont pas des documents.")
    return extension


def fichier_en_attente(cfg: Config, depot: dict) -> Path:
    return cfg.attente_depot / f"{depot['id']}{depot['extension']}"


# --- Réception et analyse ----------------------------------------------------------------------


def empreinte(chemin: Path) -> str:
    """Même calcul que l'indexation (ged.indexeur.empreinte) : repère les copies déjà indexées."""
    h = hashlib.sha256()
    with chemin.open("rb") as f:
        for bloc in iter(lambda: f.read(1024 * 1024), b""):
            h.update(bloc)
    return h.hexdigest()


def enregistrer(conn: psycopg.Connection, cfg: Config, recu: Path, nom: str, lecteur: Lecteur,
                commentaire: str = "") -> int:
    """Enregistre un fichier reçu (`recu`, dans la zone d'attente) et l'analyse. Renvoie l'id du dépôt.

    Le fichier reçu est renommé <id><extension> ; refusé, il est supprimé.
    """
    try:
        nom = nom_depose(nom)
        extension = extension_acceptee(nom)
        taille = recu.stat().st_size
        if taille == 0:
            raise DepotRefuse("Fichier vide.")
    except DepotRefuse:
        recu.unlink(missing_ok=True)
        raise
    depot_id = conn.execute(
        "INSERT INTO depot (nom, extension, taille, empreinte, depose_par, depose_par_sid, commentaire) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (nom, extension, taille, empreinte(recu), tags_manuels.auteur(lecteur), lecteur.sid or None,
         commentaire.strip()[:COMMENTAIRE_MAX] or None),
    ).fetchone()["id"]
    os.replace(recu, cfg.attente_depot / f"{depot_id}{extension}")
    conn.commit()
    try:
        analyser(conn, cfg, depot_id)
    except Exception as exc:  # le dépôt est reçu : il reste à classer, sans propositions
        log.exception("Analyse du dépôt %d impossible", depot_id)
        conn.rollback()
        conn.execute("UPDATE depot SET statut_texte = 'erreur', erreur = %s WHERE id = %s",
                     (f"{type(exc).__name__}: {exc}"[:500], depot_id))
        conn.commit()
    return depot_id


def lire_texte(cfg: Config, chemin: Path) -> Resultat:
    """Texte du fichier, avec l'OCR pour un scan quand il est configuré."""
    resultat = extraire(chemin, cfg.soffice)
    if resultat.statut == "ocr_requis" and cfg.ocr_active and cfg.tessdata:
        lu = ocr(chemin, cfg.tessdata, cfg.langue_ocr, cfg.dpi_ocr)
        if lu.statut != "erreur":
            lu.titre, lu.auteur = resultat.titre, resultat.auteur
            return lu
    return resultat


def analyser(conn: psycopg.Connection, cfg: Config, depot_id: int) -> None:
    """Texte, tags proposés et emplacements proposés du dépôt."""
    depot = conn.execute("SELECT * FROM depot WHERE id = %s", (depot_id,)).fetchone()
    resultat = lire_texte(cfg, fichier_en_attente(cfg, depot))
    contenu = resultat.contenu.replace("\x00", "")
    taxonomie = taxonomie_base.charger(conn)
    tags = [
        tags_manuels.id_tag(conn, t.categorie, t.libelle)
        for t in taxonomie.proposer(f"{resultat.titre or ''} {depot['nom']}", contenu)
    ]
    # Sans l'extension : « txt » ou « pdf » rapprocheraient tous les fichiers du même format
    nom = f"{resultat.titre or ''} {PureWindowsPath(depot['nom']).stem}"
    conn.execute(
        "UPDATE depot SET statut_texte = %s, methode = %s, titre = %s, contenu = %s, erreur = %s, "
        "tags_proposes = %s, emplacements = %s WHERE id = %s",
        (resultat.statut, resultat.methode, resultat.titre, contenu, resultat.erreur, tags,
         Jsonb(proposer_emplacements(conn, cfg, nom, contenu, tags)), depot_id),
    )
    conn.commit()


# --- Emplacements proposés ------------------------------------------------------------------------


def documents_semblables(conn: psycopg.Connection, sources: list[str], nom: str, contenu: str) -> list[dict]:
    """Documents de ces sources les plus proches du dépôt : ceux qui partagent le plus des mots
    les plus fréquents de son texte et des mots de son nom (classement plein texte)."""
    return conn.execute(
        r"""
        WITH mots AS (
            (SELECT lexeme FROM unnest(to_tsvector('ged_fr', left(%(contenu)s, 200000)))
             WHERE length(lexeme) >= 3 AND lexeme !~ '^[0-9]'
             ORDER BY cardinality(positions) DESC, lexeme LIMIT %(nb_mots)s)
            UNION
            SELECT lexeme FROM unnest(to_tsvector('ged_fr', regexp_replace(%(nom)s, '[._\-]+', ' ', 'g')))
            WHERE length(lexeme) >= 3 AND lexeme !~ '^[0-9]'
        ), requete AS (
            -- Les mots sont déjà normalisés (racines sans accents) : « simple » les garde tels quels
            SELECT to_tsquery('simple', string_agg(quote_literal(lexeme), ' | ')) AS q FROM mots
        )
        SELECT s.nom AS source, d.chemin_relatif, d.nom, ts_rank(d.recherche, r.q) AS rang
        FROM requete r, document d JOIN source s ON s.id = d.source_id
        WHERE r.q IS NOT NULL AND s.nom = ANY(%(sources)s) AND d.supprime_le IS NULL AND d.recherche @@ r.q
        ORDER BY rang DESC, d.id LIMIT %(nb_docs)s
        """,
        {"contenu": contenu, "nom": nom, "sources": sources, "nb_mots": MOTS_SEMBLABLES,
         "nb_docs": DOCUMENTS_SEMBLABLES},
    ).fetchall()


def documents_aux_memes_tags(conn: psycopg.Connection, sources: list[str], tags: list[int]) -> list[dict]:
    """Documents de ces sources qui partagent le plus de ces tags."""
    if not tags:
        return []
    return conn.execute(
        "SELECT s.nom AS source, d.chemin_relatif, d.nom, count(*) AS communs FROM document d "
        "JOIN document_tag dt ON dt.document_id = d.id JOIN source s ON s.id = d.source_id "
        "WHERE s.nom = ANY(%s) AND d.supprime_le IS NULL AND dt.tag_id = ANY(%s) "
        "GROUP BY s.nom, d.id ORDER BY communs DESC, d.modifie_le DESC LIMIT %s",
        (sources, tags, DOCUMENTS_SEMBLABLES),
    ).fetchall()


def proposer_emplacements(conn: psycopg.Connection, cfg: Config, nom: str, contenu: str,
                          tags: list[int]) -> list[dict]:
    """Dossiers proposés pour le dépôt : [{"source", "dossier", "exemples": [noms]}], le meilleur d'abord.

    Chaque document semblable vote pour son dossier, d'autant plus fort qu'il est bien classé
    (1, 1/2, 1/3...) ; les documents aux mêmes tags votent de même, pour moitié.
    """
    sources = cfg.sources_depot
    if not sources:
        return []
    votes: dict[tuple[str, str], dict] = {}
    for liste, poids in ((documents_semblables(conn, sources, nom, contenu), 1.0),
                         (documents_aux_memes_tags(conn, sources, tags), 0.5)):
        for rang, doc in enumerate(liste, start=1):
            vote = votes.setdefault((doc["source"], dossier_parent(doc["chemin_relatif"])), {"score": 0.0, "exemples": []})
            vote["score"] += poids / rang
            if len(vote["exemples"]) < EXEMPLES_MAX and doc["nom"] not in vote["exemples"]:
                vote["exemples"].append(doc["nom"])
    meilleurs = sorted(votes.items(), key=lambda v: -v[1]["score"])[:EMPLACEMENTS_MAX]
    return [{"source": source, "dossier": dossier, "exemples": v["exemples"]} for (source, dossier), v in meilleurs]


def dossiers_des_sources(conn: psycopg.Connection, cfg: Config) -> dict[str, list[str]]:
    """Dossiers connus de chaque source ouverte au dépôt (d'après l'index), pour choisir un emplacement."""
    dossiers: dict[str, set[str]] = {nom: set() for nom in cfg.sources_depot}
    lignes = conn.execute(
        "SELECT DISTINCT s.nom AS source, regexp_replace(d.chemin_relatif, %s, '') AS dossier "
        "FROM document d JOIN source s ON s.id = d.source_id "
        "WHERE s.nom = ANY(%s) AND d.supprime_le IS NULL AND strpos(d.chemin_relatif, '\\') > 0",
        (r"\\[^\\]*$", cfg.sources_depot),
    ).fetchall()
    for ligne in lignes:
        parties = ligne["dossier"].split("\\")
        # Les dossiers parents aussi, même s'ils ne contiennent que des sous-dossiers
        dossiers[ligne["source"]].update("\\".join(parties[:i]) for i in range(1, len(parties) + 1))
    return {source: sorted(liste, key=str.lower) for source, liste in dossiers.items()}


# --- Consultation -------------------------------------------------------------------------------


def depot(conn: psycopg.Connection, depot_id: int) -> dict | None:
    return conn.execute("SELECT * FROM depot WHERE id = %s", (depot_id,)).fetchone()


def est_a_lui(depot: dict, lecteur: Lecteur) -> bool:
    """Dépôt fait par cette personne (son SID, ou son compte sans identification Windows)."""
    if lecteur.sid:
        return depot["depose_par_sid"] == lecteur.sid
    return depot["depose_par"] == tags_manuels.auteur(lecteur)


def _condition_personne(lecteur: Lecteur) -> tuple[str, object]:
    if lecteur.sid:
        return "depose_par_sid = %s", lecteur.sid
    return "depose_par = %s", tags_manuels.auteur(lecteur)


COLONNES_A_CLASSER = (
    "id, nom, extension, taille, depose_par, depose_le, commentaire, statut_texte, methode, erreur, "
    "tags_proposes, emplacements, choix, envoye_le, left(contenu, 600) AS debut, length(contenu) AS longueur"
)


def _avec_copies(conn: psycopg.Connection, depots: list[dict]) -> list[dict]:
    """Ajoute à chaque dépôt les documents déjà indexés à l'identique."""
    for d in depots:
        d["copies"] = conn.execute(
            "SELECT d.id, d.chemin_relatif, s.nom AS source FROM document d JOIN source s ON s.id = d.source_id "
            "WHERE d.empreinte = (SELECT empreinte FROM depot WHERE id = %s) AND d.supprime_le IS NULL "
            "ORDER BY d.chemin LIMIT 5",
            (d["id"],),
        ).fetchall()
    return depots


def a_classer_par(conn: psycopg.Connection, lecteur: Lecteur) -> list[dict]:
    """Dépôts de la personne pas encore classés ni envoyés en validation : à classer sur sa page."""
    condition, valeur = _condition_personne(lecteur)
    return _avec_copies(conn, conn.execute(
        f"SELECT {COLONNES_A_CLASSER} FROM depot WHERE statut = 'attente' AND envoye_le IS NULL AND {condition} "
        "ORDER BY depose_le", (valeur,),
    ).fetchall())


def envoyes(conn: psycopg.Connection) -> list[dict]:
    """Dépôts envoyés en validation, les plus anciens d'abord (page À classer des administrateurs)."""
    return _avec_copies(conn, conn.execute(
        f"SELECT {COLONNES_A_CLASSER} FROM depot WHERE statut = 'attente' AND envoye_le IS NOT NULL ORDER BY envoye_le"
    ).fetchall())


def nb_envoyes(conn: psycopg.Connection) -> int:
    return conn.execute(
        "SELECT count(*) AS nb FROM depot WHERE statut = 'attente' AND envoye_le IS NOT NULL"
    ).fetchone()["nb"]


def traites(conn: psycopg.Connection, limite: int = 50) -> list[dict]:
    return conn.execute(
        "SELECT p.id, p.nom, p.depose_par, p.depose_le, p.statut, p.traite_par, p.traite_le, p.motif_rejet, "
        "p.document_id, d.chemin_relatif, s.nom AS source FROM depot p "
        "LEFT JOIN document d ON d.id = p.document_id LEFT JOIN source s ON s.id = d.source_id "
        "WHERE p.statut <> 'attente' ORDER BY p.traite_le DESC LIMIT %s",
        (limite,),
    ).fetchall()


def de_la_personne(conn: psycopg.Connection, lecteur: Lecteur, limite: int = 50) -> list[dict]:
    """Dépôts de la personne envoyés en validation ou traités, les plus récents d'abord."""
    condition, valeur = _condition_personne(lecteur)
    return conn.execute(
        "SELECT p.id, p.nom, p.depose_le, p.statut, p.traite_par, p.traite_le, p.motif_rejet, p.document_id, "
        "p.choix, d.chemin_relatif, s.nom AS source FROM depot p "
        "LEFT JOIN document d ON d.id = p.document_id LEFT JOIN source s ON s.id = d.source_id "
        f"WHERE (p.statut <> 'attente' OR p.envoye_le IS NOT NULL) AND p.{condition} "
        "ORDER BY p.depose_le DESC LIMIT %s",
        (valeur, limite),
    ).fetchall()


def options_emplacement(depot: dict) -> list[dict]:
    """Emplacements proposés au classement : le choix de la personne qui l'a envoyé en validation
    d'abord, puis ceux de l'analyse. L'ordre sert de référence au formulaire (choix par rang)."""
    options = [dict(e) for e in depot["emplacements"] or []]
    choix = depot.get("choix")
    if choix:
        cle = (choix["source"], choix["dossier"])
        options = [o for o in options if (o["source"], o["dossier"]) != cle]
        options.insert(0, {"source": choix["source"], "dossier": choix["dossier"], "exemples": [], "choix": True})
    return options


# --- Classement, validation, rejet -----------------------------------------------------------------


def _a_traiter(conn: psycopg.Connection, depot_id: int) -> dict:
    """Le dépôt, verrouillé jusqu'à la fin de la transaction (deux classements simultanés)."""
    ligne = conn.execute("SELECT * FROM depot WHERE id = %s FOR UPDATE", (depot_id,)).fetchone()
    if not ligne:
        raise DepotRefuse("Ce dépôt n'existe plus.")
    if ligne["statut"] != "attente":
        raise DepotRefuse(f"Ce dépôt a déjà été traité par {ligne['traite_par']}.")
    return ligne


def _destination(cfg: Config, ligne: dict, nom: str, source: str, dossier: str) -> tuple[Source, Path]:
    """Source et chemin complet du document classé, vérifiés (sans rien créer)."""
    cible = cfg.source_du_depot(source)
    if not cible:
        raise DepotRefuse(f"La GED ne peut pas ranger de document dans « {source} » ([depot] sources).")
    nom = nom_valide(nom)
    # L'extension ne change pas : elle dit comment lire le fichier
    if PureWindowsPath(nom).suffix.lower() != ligne["extension"]:
        nom += ligne["extension"]
    destination = cible.chemin / dossier_valide(dossier) / nom
    if destination.exists():
        raise DepotRefuse(f"Un fichier porte déjà ce nom dans ce dossier : {destination.name}")
    return cible, destination


def classer(conn: psycopg.Connection, cfg: Config, depot_id: int, lecteur: Lecteur, nom: str, source: str,
            dossier: str, tags: list[int]) -> int | None:
    """Classe le dépôt (administrateur) ou envoie ce classement en validation (autre personne,
    seulement pour son propre dépôt). Renvoie l'id du document classé, None s'il est envoyé."""
    if lecteur.admin:
        return valider(conn, cfg, depot_id, lecteur, nom, source, dossier, tags)
    ligne = _a_traiter(conn, depot_id)
    if not est_a_lui(ligne, lecteur):
        raise DepotRefuse("Ce dépôt n'est pas le vôtre.")
    _destination(cfg, ligne, nom, source, dossier)
    choix = {"nom": nom.strip(), "source": source, "dossier": dossier_valide(dossier), "tags": tags}
    conn.execute("UPDATE depot SET choix = %s, envoye_le = now() WHERE id = %s", (Jsonb(choix), depot_id))
    conn.commit()
    return None


def valider(conn: psycopg.Connection, cfg: Config, depot_id: int, lecteur: Lecteur, nom: str, source: str,
            dossier: str, tags: list[int]) -> int:
    """Copie le fichier à l'emplacement choisi, l'indexe et lui donne ces tags. Renvoie l'id du document.

    Parmi les tags proposés au dépôt, ceux qui ne sont plus cochés sont retirés pour de bon ; les
    autres tags cochés sont posés à la main. Les tags que l'emplacement apporte (taxonomie sur le
    chemin, règles de dossier) s'ajoutent.
    """
    from ged.indexeur import Indexeur

    if not lecteur.admin:
        raise DepotRefuse("Réservé aux administrateurs de la GED.")
    ligne = _a_traiter(conn, depot_id)
    cible, destination = _destination(cfg, ligne, nom, source, dossier)
    if not cible.chemin.is_dir():
        raise DepotRefuse(f"Dossier de la source « {cible.nom} » inaccessible : {cible.chemin}")
    attente = fichier_en_attente(cfg, ligne)
    if not attente.is_file():
        raise DepotRefuse("Le fichier déposé a disparu de la zone d'attente.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    # « x » : jamais d'écrasement, même si un fichier du même nom vient d'apparaître
    with attente.open("rb") as lu, destination.open("xb") as ecrit:
        shutil.copyfileobj(lu, ecrit)
    shutil.copystat(attente, destination)
    try:
        texte_ocr = None
        if ligne["methode"] == "ocr":
            texte_ocr = Resultat(statut=ligne["statut_texte"], contenu=ligne["contenu"] or "", methode="ocr",
                                 erreur=ligne["erreur"])
        document_id = Indexeur(cfg, conn).indexer_un_fichier(cible, destination, texte_ocr)
        _donner_les_tags(conn, document_id, ligne["tags_proposes"], tags, lecteur)
        conn.execute(
            "UPDATE depot SET statut = 'valide', traite_par = %s, traite_le = now(), document_id = %s, "
            "chemin_final = %s WHERE id = %s",
            (tags_manuels.auteur(lecteur), document_id, str(destination), depot_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        destination.unlink(missing_ok=True)
        raise
    attente.unlink(missing_ok=True)
    return document_id


def _donner_les_tags(conn: psycopg.Connection, document_id: int, proposes: list[int], choisis: list[int],
                     lecteur: Lecteur) -> None:
    portes = {r["tag_id"] for r in conn.execute("SELECT tag_id FROM document_tag WHERE document_id = %s", (document_id,))}
    for tag in set(choisis) - portes:
        tags_manuels.ajouter(conn, document_id, tag, lecteur)
    refuses = (set(proposes) - set(choisis)) & portes
    if refuses:
        taxonomie = taxonomie_base.charger(conn)
        for tag in refuses:
            tags_manuels.retirer(conn, document_id, tag, lecteur, taxonomie)


def rejeter(conn: psycopg.Connection, cfg: Config, depot_id: int, lecteur: Lecteur, motif: str = "") -> None:
    """Rejet par un administrateur, ou retrait par la personne qui a déposé : le fichier est supprimé
    de la zone d'attente, la ligne reste comme trace."""
    ligne = _a_traiter(conn, depot_id)
    if not lecteur.admin and not est_a_lui(ligne, lecteur):
        raise DepotRefuse("Seul un administrateur de la GED, ou la personne qui l'a déposé, peut retirer ce dépôt.")
    conn.execute(
        "UPDATE depot SET statut = 'rejete', traite_par = %s, traite_le = now(), motif_rejet = %s, contenu = NULL "
        "WHERE id = %s",
        (tags_manuels.auteur(lecteur), motif.strip()[:COMMENTAIRE_MAX] or None, depot_id),
    )
    conn.commit()
    fichier_en_attente(cfg, ligne).unlink(missing_ok=True)
