"""Accès à PostgreSQL."""

from __future__ import annotations

from importlib.resources import files

import psycopg
from psycopg.rows import dict_row

DELAI_CONNEXION = 10  # secondes


def connecter(url: str) -> psycopg.Connection:
    try:
        return psycopg.connect(url, row_factory=dict_row, connect_timeout=DELAI_CONNEXION)
    except psycopg.OperationalError as exc:
        raise SystemExit(f"Connexion à PostgreSQL impossible ([base] url dans config.toml) :\n{exc}") from exc


def initialiser(url: str) -> None:
    """Crée ou met à jour le schéma (extensions, tables, index)."""
    schema = files("ged").joinpath("schema.sql").read_text(encoding="utf-8")
    with connecter(url) as conn:
        conn.execute(schema)


# Tables vidées par « ged reinitialiser » : tout ce que l'indexation remplit
TABLES_INDEX = ("document_tag", "tag_retire", "tag_dossier", "document", "tag", "vocabulaire", "source")


def vider_index(conn: psycopg.Connection) -> None:
    """Vide l'index (documents, tags, vocabulaire, sources) en gardant le schéma. Sans commit."""
    conn.execute(f"TRUNCATE {', '.join(TABLES_INDEX)} RESTART IDENTITY CASCADE")
