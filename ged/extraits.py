"""Extraits surlignés des résultats de recherche.

Remplace ts_headline de PostgreSQL, qui analyse linguistiquement tout le texte et coûtait
environ 15 ms par long document (285 ms pour une page de 20 résultats). Ici, une expression
régulière repère les mots cherchés sans tenir compte des accents, puis on découpe le passage
qui en contient le plus.
"""

from __future__ import annotations

import html
import re
from collections import Counter
from itertools import islice

from markupsafe import Markup

from ged.texte import normaliser

# Seul le début du texte sert à fabriquer l'extrait (environ 15 pages) : au-delà, le coût
# devient sensible alors que la recherche, elle, porte toujours sur tout le document.
TAILLE_ANALYSEE = 60_000
LONGUEUR_FRAGMENT = 220
FRAGMENTS_MAX = 2
OCCURRENCES_MAX = 400

_VARIANTES = {
    "a": "aàâä", "c": "cç", "e": "eéèêë", "i": "iîï", "o": "oôö", "u": "uùûü", "y": "yÿ",
}


def _classe(lettre: str) -> str:
    return f"[{_VARIANTES[lettre]}]" if lettre in _VARIANTES else re.escape(lettre)


def construire_motif(mots: list[str]) -> re.Pattern | None:
    """Motif qui trouve chaque mot cherché, ses accents et ses terminaisons.

    On garde la racine du mot ("promesse" -> "promes") pour trouver aussi "promesses" :
    c'est ce que fait la recherche plein texte, l'extrait doit surligner les mêmes mots.
    """
    groupes = []
    for i, mot in enumerate(mots):
        n = normaliser(mot)
        if not n:
            continue
        racine = n if len(n) <= 4 else n[: max(4, len(n) - 2)]
        groupes.append(f"(?P<m{i}>(?<!\\w){''.join(_classe(c) for c in racine)}\\w*)")
    return re.compile("|".join(groupes), re.IGNORECASE) if groupes else None


def extraire(texte: str | None, motif: re.Pattern | None) -> Markup:
    """Un ou deux passages du texte autour des mots cherchés, en HTML échappé avec des <mark>."""
    if not texte:
        return Markup("")
    texte = texte[:TAILLE_ANALYSEE]
    occurrences = list(islice(motif.finditer(texte), OCCURRENCES_MAX)) if motif else []
    if not occurrences:
        return Markup(html.escape(_couper(texte, 0, LONGUEUR_FRAGMENT)))

    candidats = _fenetres(occurrences)
    candidats.sort(key=lambda c: c[0], reverse=True)

    retenus: list[tuple[int, int]] = []
    for _, debut, fin in candidats:
        if all(fin <= d or debut >= f for d, f in retenus):
            retenus.append((debut, fin))
        if len(retenus) == FRAGMENTS_MAX:
            break

    morceaux = []
    for debut, fin in sorted(retenus):
        debut, fin = _bornes_mots(texte, debut, fin)
        morceaux.append(_surligner(texte, debut, fin, occurrences))
    prefixe = "… " if sorted(retenus)[0][0] > 0 else ""
    return Markup(prefixe + " … ".join(morceaux) + " …")


def _fenetres(occurrences: list[re.Match]) -> list[tuple[tuple[int, int], int, int]]:
    """Une fenêtre candidate par occurrence, notée (mots différents présents, nombre d'occurrences).

    Fenêtre glissante en temps linéaire : deux index délimitent les occurrences contenues dans
    [début, fin[ et un compteur suit les mots distincts (un recalcul complet par fenêtre était
    quadratique : 1,6 million d'opérations pour une recherche d'une seule lettre).
    """
    candidats = []
    presents: Counter[str] = Counter()
    gauche = droite = 0
    for occ in occurrences:
        debut = max(0, occ.start() - 60)
        fin = debut + LONGUEUR_FRAGMENT
        while droite < len(occurrences) and occurrences[droite].end() <= fin:
            presents[occurrences[droite].lastgroup] += 1
            droite += 1
        while gauche < droite and occurrences[gauche].start() < debut:
            presents[occurrences[gauche].lastgroup] -= 1
            if not presents[occurrences[gauche].lastgroup]:
                del presents[occurrences[gauche].lastgroup]
            gauche += 1
        candidats.append(((len(presents), droite - gauche), debut, fin))
    return candidats


def _bornes_mots(texte: str, debut: int, fin: int) -> tuple[int, int]:
    """Élargit/rétrécit pour ne pas couper un mot en deux."""
    if debut > 0:
        espace = texte.find(" ", debut)
        debut = espace + 1 if 0 <= espace < debut + 20 else debut
    espace = texte.rfind(" ", debut, fin)
    if espace > fin - 20:
        fin = espace
    return debut, min(fin, len(texte))


def _surligner(texte: str, debut: int, fin: int, occurrences: list[re.Match]) -> str:
    morceaux, position = [], debut
    for occ in occurrences:
        if occ.start() < debut:
            continue
        if occ.end() > fin:
            break
        morceaux.append(html.escape(texte[position:occ.start()]))
        # Le texte d'origine (avec accents et majuscules), pas la version simplifiée
        morceaux.append(f"<mark>{html.escape(texte[occ.start():occ.end()])}</mark>")
        position = occ.end()
    morceaux.append(html.escape(texte[position:fin]))
    return re.sub(r"\s+", " ", "".join(morceaux)).strip()


def _couper(texte: str, debut: int, longueur: int) -> str:
    extrait = re.sub(r"\s+", " ", texte[debut:debut + longueur * 2]).strip()
    return extrait[:longueur] + (" …" if len(extrait) > longueur else "")
