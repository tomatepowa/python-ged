"""Propriétaire et droits d'accès NTFS d'un fichier (lecture seule).

Les droits sont lus tels que Windows les applique au fichier (explicites et hérités du dossier),
puis traduits dans les libellés de l'onglet Sécurité de l'explorateur : Contrôle total,
Modification, Lecture et exécution, Lecture, Écriture.

Hors Windows (tests, autre OS), les fonctions renvoient des valeurs vides.
"""

from __future__ import annotations

import logging
import sys
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

# Masques des droits "standard" de l'onglet Sécurité (winnt.h)
CONTROLE_TOTAL = 0x1F01FF
MODIFICATION = 0x1301BF
LECTURE_EXECUTION = 0x1200A9
LECTURE = 0x120089
ECRITURE = 0x100116

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
GENERIC_EXECUTE = 0x20000000
GENERIC_ALL = 0x10000000

ACE_AUTORISE = 0
ACE_REFUSE = 1
INHERIT_ONLY_ACE = 0x08  # l'entrée ne s'applique qu'aux enfants, pas à l'objet lui-même
INHERITED_ACE = 0x10


def libelle_droits(masque: int) -> str:
    """Traduit un masque d'accès en libellé de l'explorateur Windows."""
    # Les droits génériques (fréquents sur les partages) valent leurs équivalents fichiers
    if masque & GENERIC_ALL:
        masque |= CONTROLE_TOTAL
    if masque & GENERIC_READ:
        masque |= LECTURE
    if masque & GENERIC_WRITE:
        masque |= ECRITURE
    if masque & GENERIC_EXECUTE:
        masque |= LECTURE_EXECUTION & ~LECTURE | 0x20

    def contient(droits: int) -> bool:
        return masque & droits == droits

    if contient(CONTROLE_TOTAL):
        return "Contrôle total"
    if contient(MODIFICATION):
        return "Modification"
    lecture = "Lecture et exécution" if contient(LECTURE_EXECUTION) else "Lecture" if contient(LECTURE) else None
    ecriture = contient(ECRITURE)
    if lecture and ecriture:
        return f"{lecture}, écriture"
    if lecture:
        return lecture
    if ecriture:
        return "Écriture"
    return "Autorisations spéciales"


def fusionner(entrees: list[tuple[str, int, bool, bool]]) -> list[dict]:
    """Regroupe les entrées par compte et par type (autorisation / refus).

    `entrees` : (compte, masque, refus, herite). Un même compte peut apparaître plusieurs fois
    (droit explicite + droit hérité) : on cumule les masques, comme Windows.
    """
    cumul: dict[tuple[str, bool], list] = {}
    for compte, masque, refus, herite in entrees:
        cle = (compte, refus)
        if cle in cumul:
            cumul[cle][0] |= masque
            cumul[cle][1] = cumul[cle][1] and herite
        else:
            cumul[cle] = [masque, herite]
    resultat = [
        {"compte": compte, "droits": libelle_droits(masque), "refus": refus, "herite": herite}
        for (compte, refus), (masque, herite) in cumul.items()
    ]
    # Refus d'abord (ils priment), puis par nom de compte
    return sorted(resultat, key=lambda e: (not e["refus"], e["compte"].lower()))


def lire_securite(chemin: Path) -> tuple[str | None, list[dict] | None]:
    """(propriétaire, droits) du fichier. (None, None) si illisible ou hors Windows."""
    if sys.platform != "win32":
        return None, None
    import pywintypes
    import win32security

    try:
        descripteur = win32security.GetFileSecurity(
            str(chemin), win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION
        )
    except pywintypes.error as exc:
        log.debug("Sécurité illisible pour %s : %s", chemin, exc.strerror)
        return None, None

    proprietaire_sid = descripteur.GetSecurityDescriptorOwner()
    proprietaire = _nom_compte(win32security.ConvertSidToStringSid(proprietaire_sid)) if proprietaire_sid else None

    dacl = descripteur.GetSecurityDescriptorDacl()
    if dacl is None:
        # DACL nulle : aucun contrôle d'accès, tout le monde a tous les droits
        return proprietaire, [{"compte": "Tout le monde", "droits": "Contrôle total", "refus": False, "herite": False}]

    entrees = []
    for i in range(dacl.GetAceCount()):
        ace = dacl.GetAce(i)
        (type_ace, drapeaux), masque, *reste = ace
        # Seules les entrées classiques (autorisation / refus) ont la forme (en-tête, masque, SID)
        if type_ace not in (ACE_AUTORISE, ACE_REFUSE) or len(reste) != 1 or drapeaux & INHERIT_ONLY_ACE:
            continue
        compte = _nom_compte(win32security.ConvertSidToStringSid(reste[0]))
        entrees.append((compte, masque & 0xFFFFFFFF, type_ace == ACE_REFUSE, bool(drapeaux & INHERITED_ACE)))
    return proprietaire, fusionner(entrees)


@lru_cache(maxsize=10_000)
def _nom_compte(sid_texte: str) -> str:
    """'S-1-5-32-544' -> 'BUILTIN\\Administrateurs'. Le SID brut si le compte est introuvable
    (compte supprimé, domaine injoignable)."""
    import pywintypes
    import win32security

    try:
        nom, domaine, _ = win32security.LookupAccountSid(None, win32security.ConvertStringSidToSid(sid_texte))
    except pywintypes.error:
        return sid_texte
    return f"{domaine}\\{nom}" if domaine else nom
