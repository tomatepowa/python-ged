"""Reconnaissance de caractères (OCR) des PDF scannés et des images.

Utilise le moteur Tesseract intégré à PyMuPDF : aucun exécutable à installer,
seulement le fichier de langue (fra.traineddata) dans le dossier `tessdata`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from ged.extraction import Resultat
from ged.texte import detecter_code, normaliser

# Grand côté maximal de l'image rendue pour l'OCR (une page A4 à 300 dpi fait 3508 px)
PIXELS_MAX = 4000

# Qualité minimale d'un texte OCR : part des mots d'au moins 4 lettres connus du vocabulaire
# du corpus. Mesuré sur un corpus de test : photos sans texte <= 7 %, vrais scans >= 19 %.
TAUX_MOTS_CONNUS_MIN = 0.12
MOTS_CONNUS_MIN = 3
# En dessous de cette taille, le vocabulaire n'est pas représentatif : pas de filtrage
TAILLE_VOCABULAIRE_MIN = 1000


def ocr(
    chemin: Path, tessdata: str, langue: str = "fra", dpi: int = 300,
    suivi: Callable[[int, int], None] | None = None,
) -> Resultat:
    """OCR d'un PDF scanné ou d'une image. `suivi(page, nb_pages)` est appelé avant chaque page."""
    import pymupdf

    # Messages de MuPDF sur la console : ils casseraient la barre de progression. Les vraies
    # erreurs lèvent une exception et sont enregistrées dans le document.
    pymupdf.TOOLS.mupdf_display_errors(False)

    if not Path(tessdata, f"{langue}.traineddata").is_file():
        raise RuntimeError(f"Fichier de langue introuvable : {Path(tessdata, langue + '.traineddata')}")

    try:
        with pymupdf.open(chemin) as source:
            # Une image (y compris un TIFF multipage) est d'abord convertie en PDF
            if source.is_pdf:
                pdf = source
                pages = _ocr_pages(pdf, tessdata, langue, dpi, suivi)
            else:
                with pymupdf.open("pdf", source.convert_to_pdf()) as pdf:
                    pages = _ocr_pages(pdf, tessdata, langue, dpi, suivi)
    except Exception as exc:
        return Resultat(statut="erreur", methode="ocr", erreur=f"OCR : {type(exc).__name__}: {exc}"[:500])

    contenu = "\n".join(p.strip() for p in pages if p.strip())
    return Resultat(
        statut="indexe", contenu=contenu, methode="ocr", nb_pages=len(pages),
        code_doc=detecter_code(contenu[:5000]),
    )


def texte_exploitable(contenu: str, vocabulaire: set[str]) -> bool:
    """Faux si le texte OCR n'est que du bruit (photo, dessin, schéma sans texte).

    Sur une photo, Tesseract « lit » des formes au hasard : des fragments comme « RAR », « ee ET »
    ou « ÉNesN ». On compte la part de vrais mots (au moins 4 lettres, présents dans le vocabulaire
    des documents lisibles directement) parmi tous les fragments.
    """
    if len(vocabulaire) < TAILLE_VOCABULAIRE_MIN:
        return bool(contenu.strip())
    fragments = [f for f in re.findall(r"[^\W\d_]+", contenu) if len(f) >= 2]
    if not fragments:
        return False
    connus = sum(1 for f in fragments if len(f) >= 4 and normaliser(f) in vocabulaire)
    return connus >= MOTS_CONNUS_MIN and connus / len(fragments) >= TAUX_MOTS_CONNUS_MIN


def resolution_effective(largeur_pt: float, hauteur_pt: float, dpi: int) -> int:
    """Résolution de rendu plafonnée à PIXELS_MAX sur le grand côté.

    Une photo de téléphone convertie en PDF devient une "page" de plus d'un mètre (72 dpi
    supposés) : la rendre à 300 dpi produirait une image de plusieurs centaines de mégapixels.
    """
    grand_cote_pouces = max(largeur_pt, hauteur_pt) / 72
    return max(72, min(dpi, int(PIXELS_MAX / grand_cote_pouces)))


def _ocr_pages(pdf, tessdata: str, langue: str, dpi: int, suivi: Callable[[int, int], None] | None) -> list[str]:
    pages = []
    for numero, page in enumerate(pdf, start=1):
        if suivi:
            suivi(numero, pdf.page_count)
        dpi_page = resolution_effective(page.rect.width, page.rect.height, dpi)
        # full=True : la page entière est reconnue (cas des scans sans aucune couche texte)
        couche = page.get_textpage_ocr(language=langue, dpi=dpi_page, full=True, tessdata=tessdata)
        pages.append(page.get_text(textpage=couche))
    return pages
