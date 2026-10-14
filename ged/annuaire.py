"""Hiérarchie des groupes de la personne connectée, lue dans l'Active Directory.

Le jeton Windows donne tous les groupes à plat, imbrication comprise, sans dire qui contient qui.
L'annuaire le dit : l'attribut memberOf d'un compte ou d'un groupe liste ses groupes parents
directs, et l'attribut primaryGroupID d'un compte son groupe principal (en général « Utilisateurs
du domaine », absent de memberOf). Tout compte du domaine peut lire ces attributs, sans droits
particuliers.

Les groupes qui ne sont pas dans l'annuaire (groupes locaux du serveur, identités système comme
« Tout le monde ») sont classés à part. Hors domaine, il n'y a pas d'arbre.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cached_property, lru_cache

from ged.acces import Lecteur

log = logging.getLogger(__name__)

# Entrées du jeton Windows qui ne sont pas des groupes : session d'ouverture, niveau d'intégrité
_PAS_DES_GROUPES = ("S-1-5-5-", "S-1-16-")


class AnnuaireAD:
    """Groupes parents d'un compte ou d'un groupe, lus par LDAP (ADSI), gardés en mémoire une heure."""

    def __init__(self, duree_cache_s: float = 3600):
        self.duree_cache_s = duree_cache_s
        self._parents: dict[str, tuple[float, list[str]]] = {}
        self._sid_des_dn: dict[str, str] = {}

    @cached_property
    def disponible(self) -> bool:
        """Un contrôleur de domaine répond-il ? (Non sur un poste hors domaine.)"""
        if sys.platform != "win32":
            return False
        try:
            self._objet("LDAP://RootDSE")
            return True
        except Exception as exc:  # com_error : domaine introuvable
            log.info("Pas d'annuaire Active Directory joignable : %s", exc)
            return False

    def parents(self, sid: str) -> list[str]:
        """SID des groupes dont `sid` (compte ou groupe) est membre direct, groupe principal compris."""
        if not self.disponible:
            return []
        maintenant = time.monotonic()
        en_cache = self._parents.get(sid)
        if en_cache and en_cache[0] > maintenant:
            return en_cache[1]
        try:
            parents = self._lire_parents(sid)
        except Exception as exc:  # objet introuvable, annuaire injoignable
            log.warning("Groupes parents de %s illisibles : %s", sid, exc)
            parents = []
        self._parents[sid] = (maintenant + self.duree_cache_s, parents)
        return parents

    def _lire_parents(self, sid: str) -> list[str]:
        objet = self._objet(f"LDAP://<SID={sid}>")
        try:
            noms_distinctifs = list(objet.GetEx("memberOf"))
        except Exception:  # attribut vide : ADSI lève une erreur plutôt que de renvoyer une liste vide
            noms_distinctifs = []
        parents = [s for s in (self._sid_du_dn(dn) for dn in noms_distinctifs) if s]
        try:
            # Groupe principal : même domaine que le compte, numéro dans primaryGroupID
            primaire = f"{sid.rsplit('-', 1)[0]}-{int(objet.Get('primaryGroupID'))}"
            if primaire not in parents:
                parents.append(primaire)
        except Exception:  # un groupe n'a pas de groupe principal
            pass
        return parents

    def _sid_du_dn(self, dn: str) -> str | None:
        if dn not in self._sid_des_dn:
            import pywintypes
            import win32security

            try:
                brut = self._objet("LDAP://" + dn.replace("/", "\\/")).Get("objectSid")
                self._sid_des_dn[dn] = win32security.ConvertSidToStringSid(pywintypes.SID(bytes(brut)))
            except Exception as exc:
                log.warning("Groupe %s illisible : %s", dn, exc)
                return None
        return self._sid_des_dn[dn]

    @staticmethod
    def _objet(chemin: str):
        import pythoncom
        import win32com.client

        # Les pages sont servies par plusieurs fils d'exécution : COM s'initialise dans chacun
        pythoncom.CoInitialize()
        return win32com.client.GetObject(chemin)


@lru_cache(maxsize=1)
def prefixe_machine() -> str:
    """SID du « domaine » local du serveur (comptes et groupes locaux), vide hors Windows."""
    if sys.platform != "win32":
        return ""
    import pywintypes
    import win32security

    try:
        sid, _, _ = win32security.LookupAccountName(None, os.environ.get("COMPUTERNAME", ""))
        return win32security.ConvertSidToStringSid(sid)
    except pywintypes.error:
        return ""


def categorie(sid: str, machine: str) -> str:
    """« local » (BUILTIN ou groupe du serveur), « domaine » ou « systeme » (Tout le monde,
    Utilisateurs authentifiés, Autorité NT...)."""
    if sid.startswith("S-1-5-32-") or (machine and sid.startswith(machine + "-")):
        return "local"
    if sid.startswith("S-1-5-21-"):
        return "domaine"
    return "systeme"


@dataclass
class Groupe:
    """Un groupe dans l'arbre : les groupes dont il est membre sont ses `enfants` (affichés dessous)."""
    sid: str
    nom: str
    enfants: list["Groupe"] = field(default_factory=list)
    # Autres groupes (ou « directement ») par lesquels la personne en est aussi membre
    aussi_via: list[str] = field(default_factory=list)


@dataclass
class VueGroupes:
    annuaire: bool
    # Groupes dont la personne est membre directe, avec leurs parents en dessous
    arbre: list[Groupe]
    # Pour chaque groupe de l'arbre, les noms des groupes qui y mènent depuis la personne
    chemins: dict[str, list[str]]
    domaine_hors_arbre: list[str]
    locaux: list[str]
    systeme: list[str]
    refus_seulement: list[str]

    @property
    def nombre(self) -> int:
        return len(self.chemins) + len(self.domaine_hors_arbre) + len(self.locaux) + len(self.systeme)


def organiser_groupes(
    lecteur: Lecteur, parents: Callable[[str], list[str]], nommer: Callable[[str], str],
    annuaire: bool, machine: str = "",
) -> VueGroupes:
    """Groupes de la personne : arbre de l'annuaire (parcours en largeur depuis son compte, chaque
    groupe placé sous le premier chemin trouvé, le plus court) et, à part, ceux hors de l'arbre.

    Seuls les groupes de son jeton Windows comptent : un groupe de distribution ou d'une autre forêt,
    présent dans memberOf, ne donne aucun droit.
    """
    jeton = {sid for sid in lecteur.sids_refus if not sid.startswith(_PAS_DES_GROUPES)}
    racines: list[Groupe] = []
    places: dict[str, Groupe] = {}
    chemins: dict[str, list[str]] = {}
    file: deque[tuple[str, Groupe | None]] = deque([(lecteur.sid, None)]) if lecteur.sid and annuaire else deque()
    while file:
        sid, groupe = file.popleft()
        for parent in parents(sid):
            if parent not in jeton or parent == lecteur.sid:
                continue
            if parent in places:
                via = groupe.nom if groupe else "directement"
                if via not in places[parent].aussi_via and places[parent] is not groupe:
                    places[parent].aussi_via.append(via)
                continue
            nouveau = Groupe(parent, nommer(parent))
            (groupe.enfants if groupe else racines).append(nouveau)
            places[parent] = nouveau
            chemins[parent] = (chemins[groupe.sid] if groupe else []) + [nouveau.nom]
            file.append((parent, nouveau))

    def trier(groupes: list[Groupe]) -> list[Groupe]:
        for g in groupes:
            g.enfants = trier(g.enfants)
        return sorted(groupes, key=lambda g: g.nom.lower())

    reste: dict[str, list[str]] = {"domaine": [], "local": [], "systeme": []}
    for sid in lecteur.sids - places.keys() - {lecteur.sid}:
        if not sid.startswith(_PAS_DES_GROUPES):
            reste[categorie(sid, machine)].append(nommer(sid))
    refus = [nommer(sid) for sid in jeton - lecteur.sids]
    return VueGroupes(
        annuaire=annuaire,
        arbre=trier(racines),
        chemins=chemins,
        domaine_hors_arbre=sorted(reste["domaine"], key=str.lower),
        locaux=sorted(reste["local"], key=str.lower),
        systeme=sorted(reste["systeme"], key=str.lower),
        refus_seulement=sorted(refus, key=str.lower),
    )
