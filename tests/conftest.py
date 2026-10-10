"""Fabriques de documents de test (générés à la volée, aucun fichier réel)."""

from __future__ import annotations

import zipfile
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
