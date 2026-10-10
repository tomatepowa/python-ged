"""Reconnaissance de caractères (OCR) des PDF scannés et des images.

Utilise le moteur Tesseract intégré à PyMuPDF : aucun exécutable à installer,
seulement le fichier de langue (fra.traineddata) dans le dossier `tessdata`.
"""

from __future__ import annotations

from pathlib import Path

from ged.extraction import Resultat
from ged.texte import detecter_code


def ocr(chemin: Path, tessdata: str, langue: str = "fra", dpi: int = 300) -> Resultat:
    import pymupdf

    if not Path(tessdata, f"{langue}.traineddata").is_file():
        raise RuntimeError(f"Fichier de langue introuvable : {Path(tessdata, langue + '.traineddata')}")

    try:
        with pymupdf.open(chemin) as source:
            # Une image (y compris un TIFF multipage) est d'abord convertie en PDF
            if source.is_pdf:
                pdf = source
                pages = _ocr_pages(pdf, tessdata, langue, dpi)
            else:
                with pymupdf.open("pdf", source.convert_to_pdf()) as pdf:
                    pages = _ocr_pages(pdf, tessdata, langue, dpi)
    except Exception as exc:
        return Resultat(statut="erreur", methode="ocr", erreur=f"OCR : {type(exc).__name__}: {exc}"[:500])

    contenu = "\n".join(p.strip() for p in pages if p.strip())
    return Resultat(
        statut="indexe", contenu=contenu, methode="ocr", nb_pages=len(pages),
        code_doc=detecter_code(contenu[:5000]),
    )


def _ocr_pages(pdf, tessdata: str, langue: str, dpi: int) -> list[str]:
    pages = []
    for page in pdf:
        # full=True : la page entière est reconnue (cas des scans sans aucune couche texte)
        couche = page.get_textpage_ocr(language=langue, dpi=dpi, full=True, tessdata=tessdata)
        pages.append(page.get_text(textpage=couche))
    return pages
