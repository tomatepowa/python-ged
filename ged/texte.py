"""Outils de normalisation de texte partagés."""

from __future__ import annotations

import re
import unicodedata

# Code documentaire du type DSI-INFRA-NT-001-v02 : segments alphabétiques, puis un numéro.
# Les segments intermédiaires commencent par une lettre, pour écarter les dates du type SOLID-20-10-20.
# À ajuster quand la règle de la procédure des procédures sera formalisée.
MOTIF_CODE = re.compile(r"\b[A-Z]{2,6}(?:-[A-Z][A-Z0-9]{1,7}){1,3}-\d{2,4}(?:-[vV]?\d{1,3})?\b")


def sans_accents(texte: str) -> str:
    decompose = unicodedata.normalize("NFD", texte)
    return "".join(c for c in decompose if unicodedata.category(c) != "Mn")


def normaliser(texte: str) -> str:
    """Minuscules, sans accents, apostrophes typographiques unifiées."""
    return sans_accents(texte.lower()).replace("’", "'")


def detecter_code(texte: str) -> str | None:
    trouve = MOTIF_CODE.search(texte)
    return trouve.group(0) if trouve else None
