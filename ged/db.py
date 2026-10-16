"""Accès à PostgreSQL."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

DELAI_CONNEXION = 10  # secondes


def connecter(url: str) -> psycopg.Connection:
    try:
        return psycopg.connect(url, row_factory=dict_row, connect_timeout=DELAI_CONNEXION)
    except psycopg.OperationalError as exc:
        raise SystemExit(f"Connexion à PostgreSQL impossible (connexion.toml, ou « ged serveur --assistant ») :\n{exc}") from exc


def initialiser(url: str, taxonomie: Path | None = None) -> None:
    """Crée ou met à jour le schéma (extensions, tables, index). Avec `taxonomie` (taxonomie.toml) :
    l'importe si la base n'a encore jamais reçu de taxonomie."""
    schema = files("ged").joinpath("schema.sql").read_text(encoding="utf-8")
    with connecter(url) as conn:
        conn.execute(schema)
        if taxonomie is not None:
            from ged.taxonomie_base import importer_au_premier_demarrage

            try:
                importer_au_premier_demarrage(conn, taxonomie)
            except ValueError as exc:
                raise SystemExit(f"{taxonomie} illisible, taxonomie non importée : {exc}") from exc


# Tables vidées par « ged reinitialiser » : tout ce que l'indexation remplit. Les tags et leurs
# mots-clés (la taxonomie), les réglages et le journal restent.
TABLES_INDEX = ("document_tag", "tag_retire", "tag_dossier", "document", "vocabulaire", "source")


def vider_index(conn: psycopg.Connection) -> None:
    """Vide l'index (documents, poses de tags, vocabulaire, sources) en gardant le schéma et la
    taxonomie. Sans commit."""
    conn.execute(f"TRUNCATE {', '.join(TABLES_INDEX)} RESTART IDENTITY CASCADE")
