"""Requêtes de recherche plein texte."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

import psycopg
from markupsafe import Markup

PAR_PAGE = 20

# Marqueurs de surlignage neutres : le texte est échappé AVANT d'être transformé en <mark>
_DEBUT, _FIN = "\x02", "\x03"
_OPTIONS_EXTRAIT = f"StartSel={_DEBUT}, StopSel={_FIN}, MaxFragments=2, MaxWords=30, MinWords=12, FragmentDelimiter=\" … \""


@dataclass
class Filtres:
    q: str = ""
    pole: str = ""
    famille: str = ""
    tags: list[int] = field(default_factory=list)
    page: int = 1


@dataclass
class Resultats:
    documents: list[dict]
    total: int
    # exact | prefixe | approchant | recents
    mode: str


def surligner(extrait: str | None) -> Markup:
    """Échappe l'extrait renvoyé par PostgreSQL et convertit les marqueurs en <mark>."""
    if not extrait:
        return Markup("")
    sur = html.escape(extrait).replace(_DEBUT, "<mark>").replace(_FIN, "</mark>")
    return Markup(sur)


def requete_prefixe(q: str) -> str | None:
    """'imprim reseau' -> 'imprim:* & reseau:*' : trouve les mots commençant par ce qui est tapé."""
    mots = re.findall(r"\w+", q)
    return " & ".join(f"{m}:*" for m in mots) or None


def rechercher(conn: psycopg.Connection, filtres: Filtres) -> Resultats:
    q = filtres.q.strip()
    if not q:
        return _executer(conn, filtres, mode="recents")
    for mode in ("exact", "prefixe", "approchant"):
        resultats = _executer(conn, filtres, mode)
        if resultats.total:
            return resultats
    return resultats


def _executer(conn: psycopg.Connection, filtres: Filtres, mode: str) -> Resultats:
    parametres: dict = {
        "q": filtres.q.strip(),
        "limite": PAR_PAGE,
        "decalage": (max(filtres.page, 1) - 1) * PAR_PAGE,
        "options": _OPTIONS_EXTRAIT,
    }
    conditions = ["d.supprime_le IS NULL"]

    if mode == "exact":
        requete_ts = "websearch_to_tsquery('ged_fr', %(q)s)"
    elif mode == "prefixe":
        prefixe = requete_prefixe(filtres.q)
        if not prefixe:
            return Resultats([], 0, mode)
        parametres["prefixe"] = prefixe
        requete_ts = "to_tsquery('ged_fr', %(prefixe)s)"
    else:
        requete_ts = None

    if requete_ts:
        conditions.append(f"d.recherche @@ {requete_ts}")
        rang = f"ts_rank_cd(d.recherche, {requete_ts})"
        extrait = f"ts_headline('ged_fr', left(coalesce(d.contenu, ''), 100000), {requete_ts}, %(options)s)"
    elif mode == "approchant":
        # Tolérance aux fautes de frappe, sur le nom de fichier et le titre
        # "<%%" est l'opérateur pg_trgm "<%" (le % est doublé pour psycopg)
        conditions.append("(%(q)s <%% d.nom OR %(q)s <%% coalesce(d.titre, ''))")
        rang = "greatest(word_similarity(%(q)s, d.nom), word_similarity(%(q)s, coalesce(d.titre, '')))"
        extrait = "left(coalesce(d.contenu, ''), 250)"
    else:  # recents
        rang = "0"
        extrait = "left(coalesce(d.contenu, ''), 250)"

    if filtres.pole:
        conditions.append("s.pole = %(pole)s")
        parametres["pole"] = filtres.pole
    if filtres.famille:
        conditions.append("d.famille = %(famille)s")
        parametres["famille"] = filtres.famille
    if filtres.tags:
        # Le document doit porter TOUS les tags sélectionnés
        conditions.append(
            "d.id IN (SELECT document_id FROM document_tag WHERE tag_id = ANY(%(tags)s) "
            "GROUP BY document_id HAVING count(*) = %(nb_tags)s)"
        )
        parametres["tags"] = filtres.tags
        parametres["nb_tags"] = len(set(filtres.tags))

    sql = f"""
        SELECT d.id, d.nom, d.titre, d.chemin, d.chemin_relatif, d.famille, d.extension, d.statut,
               d.code_doc, d.modifie_le, d.taille, s.pole,
               {extrait} AS extrait,
               {rang} AS rang,
               count(*) OVER () AS total,
               coalesce((SELECT json_agg(json_build_object('categorie', t.categorie, 'libelle', t.libelle)
                                         ORDER BY t.categorie, t.libelle)
                         FROM document_tag dt JOIN tag t ON t.id = dt.tag_id
                         WHERE dt.document_id = d.id), '[]') AS tags
        FROM document d
        JOIN source s ON s.id = d.source_id
        WHERE {' AND '.join(conditions)}
        ORDER BY rang DESC, d.modifie_le DESC
        LIMIT %(limite)s OFFSET %(decalage)s
    """
    lignes = conn.execute(sql, parametres).fetchall()
    for ligne in lignes:
        ligne["extrait"] = surligner(ligne["extrait"]) if requete_ts else ligne["extrait"]
    total = lignes[0]["total"] if lignes else 0
    return Resultats(lignes, total, mode)


def facettes(conn: psycopg.Connection) -> dict:
    """Valeurs disponibles pour les filtres, avec le nombre de documents."""
    actifs = "d.supprime_le IS NULL"
    return {
        "poles": conn.execute(
            f"SELECT s.pole AS valeur, count(*) AS nb FROM document d JOIN source s ON s.id = d.source_id "
            f"WHERE {actifs} GROUP BY s.pole ORDER BY s.pole"
        ).fetchall(),
        "familles": conn.execute(
            f"SELECT famille AS valeur, count(*) AS nb FROM document d WHERE {actifs} "
            f"GROUP BY famille ORDER BY nb DESC"
        ).fetchall(),
        "tags": conn.execute(
            f"SELECT t.id, t.categorie, t.libelle, count(*) AS nb FROM tag t "
            f"JOIN document_tag dt ON dt.tag_id = t.id JOIN document d ON d.id = dt.document_id "
            f"WHERE {actifs} GROUP BY t.id ORDER BY t.categorie, t.libelle"
        ).fetchall(),
    }


def statistiques(conn: psycopg.Connection) -> dict:
    return {
        "par_statut": conn.execute(
            "SELECT statut, count(*) AS nb FROM document WHERE supprime_le IS NULL GROUP BY statut ORDER BY nb DESC"
        ).fetchall(),
        "par_source": conn.execute(
            "SELECT s.nom, s.pole, s.chemin, count(d.id) AS nb, max(d.indexe_le) AS derniere_indexation "
            "FROM source s LEFT JOIN document d ON d.source_id = s.id AND d.supprime_le IS NULL "
            "GROUP BY s.id ORDER BY s.nom"
        ).fetchall(),
        "erreurs": conn.execute(
            "SELECT id, chemin_relatif, erreur FROM document "
            "WHERE statut = 'erreur' AND supprime_le IS NULL ORDER BY chemin_relatif LIMIT 100"
        ).fetchall(),
    }
