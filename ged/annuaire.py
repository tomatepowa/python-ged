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


@dataclass
class Noeud:
    """Un compte ou un groupe tel que lu dans l'annuaire."""
    dn: str | None                       # None : SID absent de l'annuaire (groupe local, autre domaine)
    parents_dn: list[str] = field(default_factory=list)   # memberOf
    principal: str | None = None         # SID du groupe principal (comptes seulement)
    expire: float = 0.0


class AnnuaireAD:
    """Groupes parents des comptes et groupes, lus dans l'annuaire par recherches LDAP groupées.

    Une seule recherche pour un lot de SID ramène, pour chacun, son nom LDAP (DN), ses groupes
    parents (memberOf, des DN) et son groupe principal. Tous les groupes d'une personne étant
    cherchés ensemble, leurs DN suffisent à traduire les parents en SID, sans autre appel. Les
    résultats sont gardés en mémoire une heure.
    """

    TAILLE_LOT = 100   # SID par recherche (filtre LDAP de taille raisonnable)

    def __init__(self, duree_cache_s: float = 3600):
        self.duree_cache_s = duree_cache_s
        self._noeuds: dict[str, Noeud] = {}

    @cached_property
    def disponible(self) -> bool:
        """Un contrôleur de domaine répond-il ? (Non sur un poste hors domaine.)"""
        if sys.platform != "win32":
            return False
        try:
            self._contexte_de_nommage
            return True
        except Exception as exc:  # com_error : domaine introuvable
            log.info("Pas d'annuaire Active Directory joignable : %s", exc)
            return False

    def graphe(self, sids: set[str] | frozenset[str]) -> dict[str, list[str]]:
        """Pour chaque SID connu de l'annuaire, les SID de ses groupes parents directs parmi `sids`
        (groupe principal compris)."""
        if not self.disponible:
            return {}
        maintenant = time.monotonic()
        # Seuls les SID d'un domaine peuvent être dans l'annuaire (pas Tout le monde, BUILTIN...)
        candidats = {s for s in sids if s.startswith("S-1-5-21-")}
        manquants = sorted(s for s in candidats if s not in self._noeuds or self._noeuds[s].expire < maintenant)
        if manquants:
            debut = time.perf_counter()
            for i in range(0, len(manquants), self.TAILLE_LOT):
                lot = manquants[i:i + self.TAILLE_LOT]
                try:
                    trouves = {e["sid"]: e for e in self._rechercher(lot)}
                except Exception as exc:  # annuaire injoignable en cours de route
                    log.warning("Lecture de l'annuaire impossible : %s", exc)
                    trouves = {}
                for sid in lot:
                    e = trouves.get(sid)
                    self._noeuds[sid] = Noeud(
                        dn=e["dn"] if e else None,
                        parents_dn=list(e.get("member_of") or []) if e else [],
                        principal=f"{sid.rsplit('-', 1)[0]}-{e['primary_group_id']}" if e and e.get("primary_group_id") else None,
                        expire=maintenant + self.duree_cache_s,
                    )
            log.info("Annuaire : %d comptes et groupes lus en %.1f s", len(manquants), time.perf_counter() - debut)

        sid_du_dn = {self._noeuds[s].dn.lower(): s for s in candidats if self._noeuds[s].dn}
        graphe = {}
        for sid in candidats:
            noeud = self._noeuds[sid]
            parents = [sid_du_dn[dn.lower()] for dn in noeud.parents_dn if dn.lower() in sid_du_dn]
            if noeud.principal and noeud.principal not in parents:
                parents.append(noeud.principal)
            graphe[sid] = parents
        return graphe

    def _rechercher(self, sids: list[str]) -> list[dict]:
        """Recherche LDAP (ADSI) des objets de ces SID : [{"sid", "dn", "member_of", "primary_group_id"}]."""
        import pythoncom
        import pywintypes
        import win32com.client
        import win32security

        # Les pages sont servies par plusieurs fils d'exécution : COM s'initialise dans chacun
        pythoncom.CoInitialize()
        filtre = "(|" + "".join(f"(objectSid={sid})" for sid in sids) + ")"
        connexion = win32com.client.Dispatch("ADODB.Connection")
        connexion.Provider = "ADsDSOObject"
        connexion.Open("Active Directory Provider")
        try:
            commande = win32com.client.Dispatch("ADODB.Command")
            commande.ActiveConnection = connexion
            commande.Properties("Page Size").Value = 500
            commande.CommandText = (
                f"<LDAP://{self._contexte_de_nommage}>;{filtre};"
                "objectSid,distinguishedName,memberOf,primaryGroupID;subtree"
            )
            resultats, _ = commande.Execute()
            entrees = []
            while not resultats.EOF:
                champ = resultats.Fields
                entrees.append({
                    "sid": win32security.ConvertSidToStringSid(pywintypes.SID(bytes(champ("objectSid").Value))),
                    "dn": champ("distinguishedName").Value,
                    "member_of": champ("memberOf").Value or (),
                    "primary_group_id": champ("primaryGroupID").Value,
                })
                resultats.MoveNext()
            return entrees
        finally:
            connexion.Close()

    @cached_property
    def _contexte_de_nommage(self) -> str:
        """Racine du domaine dans l'annuaire (« DC=oc-sante,DC=fr »)."""
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        return win32com.client.GetObject("LDAP://RootDSE").Get("defaultNamingContext")


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
