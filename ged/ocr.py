"""Reconnaissance de caractères (OCR) des PDF scannés et des images.

Utilise le moteur Tesseract intégré à PyMuPDF : aucun exécutable à installer,
seulement le fichier de langue (fra.traineddata) dans le dossier `tessdata`.
"""

from __future__ import annotations

from pathlib import Path

from ged.extraction import Resultat
from ged.texte import detecter_code

# Grand côté maximal de l'image rendue pour l'OCR (une page A4 à 300 dpi fait 3508 px)
PIXELS_MAX = 4000


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


def resolution_effective(largeur_pt: float, hauteur_pt: float, dpi: int) -> int:
    """Résolution de rendu plafonnée à PIXELS_MAX sur le grand côté.

    Une photo de téléphone convertie en PDF devient une "page" de plus d'un mètre (72 dpi
    supposés) : la rendre à 300 dpi produirait une image de plusieurs centaines de mégapixels.
    """
    grand_cote_pouces = max(largeur_pt, hauteur_pt) / 72
    return max(72, min(dpi, int(PIXELS_MAX / grand_cote_pouces)))


def _ocr_pages(pdf, tessdata: str, langue: str, dpi: int) -> list[str]:
    pages = []
    for page in pdf:
        dpi_page = resolution_effective(page.rect.width, page.rect.height, dpi)
        # full=True : la page entière est reconnue (cas des scans sans aucune couche texte)
        couche = page.get_textpage_ocr(language=langue, dpi=dpi_page, full=True, tessdata=tessdata)
        pages.append(page.get_text(textpage=couche))
    return pages
