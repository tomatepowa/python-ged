"""Propriétaire et droits d'accès NTFS d'un fichier (lecture seule).

Les droits sont lus tels que Windows les applique au fichier (explicites et hérités du dossier),
puis traduits dans les libellés de l'onglet Sécurité de l'explorateur : Contrôle total,
Modification, Lecture et exécution, Lecture, Écriture.

Pour filtrer les résultats selon la personne connectée (ged.acces), on garde aussi les SID des
comptes et groupes autorisés à lire, et l'ordre des entrées de lecture quand un refus en fait partie.

Hors Windows (tests, autre OS), les fonctions renvoient des valeurs vides.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PureWindowsPath

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

# Droit de lire le contenu d'un fichier : c'est lui qui décide de l'accès dans la GED
FILE_READ_DATA = 0x1
TOUT_LE_MONDE = "S-1-1-0"

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


def donne_lecture(masque: int) -> bool:
    """L'entrée porte-t-elle sur la lecture du contenu (directement ou par un droit générique) ?"""
    return bool(masque & (FILE_READ_DATA | GENERIC_READ | GENERIC_ALL))


def acces_lecture(entrees: list[tuple[str, int, bool]]) -> tuple[list[str], list[list[str]] | None]:
    """(SID autorisés à lire, ordre des entrées de lecture) d'après les entrées de la DACL.

    `entrees` : (SID, masque, refus), dans l'ordre de la DACL. Windows applique la première
    entrée qui concerne la personne (refus explicite, autorisation explicite, refus hérité,
    autorisation héritée) : l'ordre n'est gardé que s'il y a un refus, sinon un seul SID
    autorisé suffit. Ordre : [[SID, "A" (autorisé) ou "R" (refusé)], ...]
    """
    lecture = [(sid, "R" if refus else "A") for sid, masque, refus in entrees if donne_lecture(masque)]
    sids = list(dict.fromkeys(sid for sid, sens in lecture if sens == "A"))
    ordre = [[sid, sens] for sid, sens in lecture] if any(sens == "R" for _, sens in lecture) else None
    return sids, ordre


@dataclass
class Securite:
    """Sécurité d'un fichier ; tout est None si elle n'a pas pu être lue (le document est alors
    masqué aux personnes qui ne sont pas administrateurs de la GED)."""
    proprietaire: str | None = None
    # Pour l'affichage : [{"compte": "DOMAINE\groupe", "droits": "Lecture", "refus": False, "herite": True}]
    droits: list[dict] | None = None
    # Pour le filtrage : voir acces_lecture()
    acces_sids: list[str] | None = None
    acces_ordre: list[list[str]] | None = None


def lire_securite(chemin: Path) -> Securite:
    """Propriétaire et droits du fichier (Securite vide si illisible ou hors Windows)."""
    if sys.platform != "win32":
        return Securite()
    import pywintypes
    import win32security

    try:
        descripteur = win32security.GetFileSecurity(
            str(chemin), win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION
        )
    except pywintypes.error as exc:
        log.debug("Sécurité illisible pour %s : %s", chemin, exc.strerror)
        return Securite()

    proprietaire_sid = descripteur.GetSecurityDescriptorOwner()
    proprietaire = _nom_compte(win32security.ConvertSidToStringSid(proprietaire_sid)) if proprietaire_sid else None

    dacl = descripteur.GetSecurityDescriptorDacl()
    if dacl is None:
        # DACL nulle : aucun contrôle d'accès, tout le monde a tous les droits
        return Securite(
            proprietaire,
            [{"compte": "Tout le monde", "droits": "Contrôle total", "refus": False, "herite": False}],
            [TOUT_LE_MONDE],
            None,
        )
    entrees = entrees_dacl(dacl)
    sids, ordre = acces_lecture([(sid, masque, refus) for sid, masque, refus, _ in entrees])
    droits = fusionner([(_nom_compte(sid), masque, refus, herite) for sid, masque, refus, herite in entrees])
    return Securite(proprietaire, droits, sids, ordre)


def entrees_dacl(dacl) -> list[tuple[str, int, bool, bool]]:
    """(SID, masque, refus, hérité) des entrées d'une DACL qui s'appliquent à l'objet, dans l'ordre."""
    import win32security

    entrees = []
    for i in range(dacl.GetAceCount()):
        (type_ace, drapeaux), masque, *reste = dacl.GetAce(i)
        # Seules les entrées classiques (autorisation / refus) ont la forme (en-tête, masque, SID)
        if type_ace not in (ACE_AUTORISE, ACE_REFUSE) or len(reste) != 1 or drapeaux & INHERIT_ONLY_ACE:
            continue
        sid = win32security.ConvertSidToStringSid(reste[0])
        entrees.append((sid, masque & 0xFFFFFFFF, type_ace == ACE_REFUSE, bool(drapeaux & INHERITED_ACE)))
    return entrees


def lire_partage(chemin: Path | str) -> list[list[str]] | None:
    r"""Droits de lecture du partage réseau qui contient `chemin` (\\serveur\partage\...).

    Même format que Securite.acces_ordre. None si `chemin` n'est pas un partage UNC ou si ses
    droits sont illisibles : il faut en général être administrateur du serveur de fichiers.
    """
    racine = PureWindowsPath(chemin).drive
    if sys.platform != "win32" or not racine.startswith("\\\\"):
        return None
    import pywintypes
    import win32net

    serveur, _, partage = racine.strip("\\").partition("\\")
    try:
        descripteur = win32net.NetShareGetInfo(serveur, partage, 502).get("security_descriptor")
    except pywintypes.error as exc:
        log.warning("Droits du partage %s illisibles (%s) : seuls les droits NTFS seront appliqués", racine, exc.strerror)
        return None
    if descripteur is None:
        return None
    dacl = descripteur.GetSecurityDescriptorDacl()
    if dacl is None:
        return [[TOUT_LE_MONDE, "A"]]
    return [[sid, "R" if refus else "A"] for sid, masque, refus, _ in entrees_dacl(dacl) if donne_lecture(masque)]


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
