"""Lecture d'un document de l'index : fiche, fichier d'origine, tags et sources."""

from __future__ import annotations

import psycopg

from ged.acces import FiltreAcces


def nom_source(conn: psycopg.Connection, source_id: int) -> str | None:
    ligne = conn.execute("SELECT nom FROM source WHERE id = %s", (source_id,)).fetchone()
    return ligne["nom"] if ligne else None


def tags_choisis(conn: psycopg.Connection, ids: list[int]) -> list[dict]:
    """Tags sélectionnés dans les filtres, pour leurs pastilles au-dessus des résultats."""
    if not ids:
        return []
    return conn.execute(
        "SELECT id, categorie, libelle FROM tag WHERE id = ANY(%s) ORDER BY categorie, libelle", (ids,)
    ).fetchall()


def fiche(conn: psycopg.Connection, document_id: int, acces: FiltreAcces) -> dict | None:
    """Document, ses tags et ses doublons (même contenu ailleurs) : {"doc", "tags", "doublons"}.

    Un document hors des droits de la personne donne None, comme s'il n'existait pas.
    """
    doc = conn.execute(
        "SELECT d.*, s.nom AS source, s.pole FROM document d JOIN source s ON s.id = d.source_id "
        f"WHERE d.id = %(id)s AND {acces.sql}",
        {"id": document_id, **acces.parametres},
    ).fetchone()
    if not doc:
        return None
    tags = conn.execute(
        "SELECT t.id, t.categorie, t.libelle, dt.origine FROM document_tag dt JOIN tag t ON t.id = dt.tag_id "
        "WHERE dt.document_id = %s ORDER BY t.categorie, t.libelle",
        (document_id,),
    ).fetchall()
    doublons = conn.execute(
        "SELECT d.id, d.chemin_relatif FROM document d "
        f"WHERE d.empreinte = %(empreinte)s AND d.id <> %(id)s AND d.supprime_le IS NULL AND {acces.sql} "
        "ORDER BY d.chemin_relatif",
        {"empreinte": doc["empreinte"], "id": document_id, **acces.parametres},
    ).fetchall() if doc["empreinte"] else []
    return {"doc": doc, "tags": tags, "doublons": doublons}


def fichier(conn: psycopg.Connection, document_id: int, acces: FiltreAcces) -> dict | None:
    """Chemin, nom et extension du fichier d'origine, si la personne a le droit de le lire."""
    return conn.execute(
        "SELECT d.chemin, d.nom, d.extension FROM document d "
        f"WHERE d.id = %(id)s AND d.supprime_le IS NULL AND {acces.sql}",
        {"id": document_id, **acces.parametres},
    ).fetchone()
