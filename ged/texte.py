"""Outils de normalisation de texte partagés."""

from __future__ import annotations

import re
import unicodedata

# Code documentaire du type DSI-INFRA-NT-001-v02 ou NT.OC.GSI.01 : segments alphabétiques, puis un numéro,
# séparés par "-", "." ou "_". Les segments intermédiaires commencent par une lettre, pour écarter les dates
# du type SOLID-20-10-20. Pas de \b : "_" compte comme une lettre pour lui (NT_OC_GSI_01_procedure).
# À ajuster quand la règle de la procédure des procédures sera formalisée.
MOTIF_CODE = re.compile(
    r"(?<![A-Za-z0-9])[A-Z]{2,6}(?:[-._][A-Z][A-Z0-9]{1,7}){1,3}[-._]\d{2,4}(?:[-._][vV]?\d{1,3})?(?![A-Za-z0-9])"
)


def sans_accents(texte: str) -> str:
    decompose = unicodedata.normalize("NFD", texte)
    return "".join(c for c in decompose if unicodedata.category(c) != "Mn")


def normaliser(texte: str) -> str:
    """Minuscules, sans accents, apostrophes typographiques unifiées."""
    return sans_accents(texte.lower()).replace("’", "'")


def detecter_code(texte: str) -> str | None:
    trouve = MOTIF_CODE.search(texte)
    return trouve.group(0) if trouve else None


def code_document(chemin: str, contenu: str) -> str | None:
    """Code documentaire : d'abord dans le nom du fichier, sinon dans le début du contenu (en-tête)."""
    nom = re.split(r"[\\/]", chemin)[-1]
    return detecter_code(nom) or detecter_code(contenu[:5000])
