"""Hiérarchie des groupes (Active Directory simulé)."""

from __future__ import annotations

import sys

import pytest

from ged.acces import Lecteur
from ged.annuaire import AnnuaireAD, categorie, organiser_groupes

DOMAINE = "S-1-5-21-10-20-30"
MACHINE = "S-1-5-21-99-98-97"
MOI = f"{DOMAINE}-1105"
UTILISATEURS_DOMAINE = f"{DOMAINE}-513"
DSI_INFRA = f"{DOMAINE}-2001"
DSI = f"{DOMAINE}-2002"
GED_LECTURE = f"{DOMAINE}-2003"
DIFFUSION = f"{DOMAINE}-2004"      # liste de diffusion : dans memberOf, pas dans le jeton
HORS_ARBRE = f"{DOMAINE}-2005"     # dans le jeton, mais l'annuaire ne dit pas d'où il vient
BUILTIN_UTILISATEURS = "S-1-5-32-545"
LOCAL_DOCKER = f"{MACHINE}-1003"
TOUT_LE_MONDE = "S-1-1-0"
AUTHENTIFIES = "S-1-5-11"
ADMINS = "S-1-5-32-544"
SESSION = "S-1-5-5-0-4242"

NOMS = {
    UTILISATEURS_DOMAINE: "CLINIQUES\\Utilisateurs du domaine", DSI_INFRA: "CLINIQUES\\DSI-Infra",
    DSI: "CLINIQUES\\DSI", GED_LECTURE: "CLINIQUES\\GED-Lecture", HORS_ARBRE: "CLINIQUES\\Projet X",
    BUILTIN_UTILISATEURS: "BUILTIN\\Utilisateurs", LOCAL_DOCKER: "VM-GED\\docker-users",
    TOUT_LE_MONDE: "Tout le monde", AUTHENTIFIES: "AUTORITE NT\\Utilisateurs authentifiés",
    ADMINS: "BUILTIN\\Administrateurs",
}

# memberOf (et groupe principal) : qui est membre de quoi
PARENTS = {
    MOI: [DSI_INFRA, DIFFUSION, UTILISATEURS_DOMAINE],
    DSI_INFRA: [DSI],
    DSI: [GED_LECTURE, DSI_INFRA],          # imbrication circulaire, permise par l'AD
    UTILISATEURS_DOMAINE: [GED_LECTURE],     # GED-Lecture atteint par deux chemins
}

ACTIFS = {MOI, UTILISATEURS_DOMAINE, DSI_INFRA, DSI, GED_LECTURE, HORS_ARBRE, BUILTIN_UTILISATEURS,
          LOCAL_DOCKER, TOUT_LE_MONDE, AUTHENTIFIES, SESSION}
LECTEUR = Lecteur("CLINIQUES\\acosson", frozenset(ACTIFS), frozenset(ACTIFS | {ADMINS}), sid=MOI)


def vue(annuaire=True):
    return organiser_groupes(LECTEUR, lambda sid: PARENTS.get(sid, []), lambda sid: NOMS.get(sid, sid), annuaire, MACHINE)


def test_arbre_depuis_le_compte():
    v = vue()
    # Groupes directs, triés ; la liste de diffusion (hors jeton) n'apparaît pas
    assert [g.nom for g in v.arbre] == ["CLINIQUES\\DSI-Infra", "CLINIQUES\\Utilisateurs du domaine"]
    dsi_infra, utilisateurs = v.arbre
    assert [g.nom for g in dsi_infra.enfants] == ["CLINIQUES\\DSI"]
    # GED-Lecture est placé une fois, par le chemin le plus court, et signale l'autre
    assert [g.nom for g in utilisateurs.enfants] == ["CLINIQUES\\GED-Lecture"]
    assert dsi_infra.enfants[0].enfants == []
    assert utilisateurs.enfants[0].aussi_via == ["CLINIQUES\\DSI"]


def test_chemins_vers_chaque_groupe():
    chemins = vue().chemins
    assert chemins[DSI] == ["CLINIQUES\\DSI-Infra", "CLINIQUES\\DSI"]
    assert chemins[GED_LECTURE] == ["CLINIQUES\\Utilisateurs du domaine", "CLINIQUES\\GED-Lecture"]


def test_groupes_hors_annuaire_classes():
    v = vue()
    assert v.domaine_hors_arbre == ["CLINIQUES\\Projet X"]
    assert v.locaux == ["BUILTIN\\Utilisateurs", "VM-GED\\docker-users"]
    # Ni le compte lui-même, ni la session d'ouverture
    assert v.systeme == ["AUTORITE NT\\Utilisateurs authentifiés", "Tout le monde"]
    assert v.refus_seulement == ["BUILTIN\\Administrateurs"]
    assert v.nombre == 4 + 1 + 2 + 2


def test_hors_domaine_pas_d_arbre():
    v = vue(annuaire=False)
    assert v.arbre == [] and v.chemins == {}
    # Les groupes du domaine restent listés, à plat
    assert len(v.domaine_hors_arbre) == 5


@pytest.mark.parametrize(
    "sid, attendu",
    [(BUILTIN_UTILISATEURS, "local"), (LOCAL_DOCKER, "local"), (DSI, "domaine"),
     (TOUT_LE_MONDE, "systeme"), (AUTHENTIFIES, "systeme"), ("S-1-11-96-3623454863", "systeme")],
)
def test_categorie(sid, attendu):
    assert categorie(sid, MACHINE) == attendu


@pytest.mark.skipif(sys.platform != "win32", reason="Active Directory : Windows uniquement")
def test_annuaire_reel_sans_erreur():
    """Sur un poste du domaine, l'annuaire répond ; ailleurs, aucun parent, sans exception."""
    annuaire = AnnuaireAD()
    if not annuaire.disponible:
        assert annuaire.parents("S-1-5-21-1-2-3-1001") == []
