"""Fabriques de documents de test (générés à la volée, aucun fichier réel) et base de test."""

from __future__ import annotations

import os
import zipfile
from importlib.resources import files
from pathlib import Path

import pytest

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def paragraphe(texte: str, style: str | None = None) -> str:
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{ppr}<w:r><w:t>{texte}</w:t></w:r></w:p>"


@pytest.fixture
def fabriquer_docx(tmp_path: Path):
    def fabriquer(nom: str, paragraphes: list[str], entete: str = "", titre: str = "", auteur: str = "") -> Path:
        chemin = tmp_path / nom
        chemin.parent.mkdir(parents=True, exist_ok=True)
        corps = "".join(paragraphe(p) for p in paragraphes)
        with zipfile.ZipFile(chemin, "w") as z:
            z.writestr(
                "docProps/core.xml",
                '<?xml version="1.0"?><cp:coreProperties '
                'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                f'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{titre}</dc:title>'
                f"<dc:creator>{auteur}</dc:creator><cp:lastModifiedBy>Tech B</cp:lastModifiedBy></cp:coreProperties>",
            )
            z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document {W}><w:body>{corps}</w:body></w:document>')
            if entete:
                z.writestr("word/header1.xml", f'<?xml version="1.0"?><w:hdr {W}>{paragraphe(entete)}</w:hdr>')
                z.writestr("word/header2.xml", f'<?xml version="1.0"?><w:hdr {W}>{paragraphe(entete)}</w:hdr>')
        return chemin

    return fabriquer


@pytest.fixture
def conn():
    """Connexion à la base PostgreSQL de test (GED_TEST_BASE), vidée, dans une transaction annulée."""
    url = os.environ.get("GED_TEST_BASE")
    if not url:
        pytest.skip("GED_TEST_BASE non défini (base PostgreSQL de test)")
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(url, row_factory=dict_row) as connexion:
        try:
            connexion.execute(files("ged").joinpath("schema.sql").read_text(encoding="utf-8"))
            connexion.execute("TRUNCATE document_tag, tag_retire, tag_dossier, journal_tag, document, tag, "
                              "vocabulaire, source, reglage RESTART IDENTITY CASCADE")
            yield connexion
        finally:
            connexion.rollback()
