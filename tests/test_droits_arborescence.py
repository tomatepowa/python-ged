import sys
from datetime import datetime, timezone

import pytest

from ged.arborescence import construire_arbre, dossier_parent, fil_ariane, trouver
from ged.droits import CONTROLE_TOTAL, GENERIC_ALL, LECTURE, LECTURE_EXECUTION, MODIFICATION, fusionner, libelle_droits
from ged.recherche import Filtres


@pytest.mark.parametrize(
    "masque, attendu",
    [
        (CONTROLE_TOTAL, "Contrôle total"),
        (GENERIC_ALL, "Contrôle total"),
        (MODIFICATION, "Modification"),
        (LECTURE_EXECUTION, "Lecture et exécution"),
        (LECTURE, "Lecture"),
        (LECTURE | 0x100116, "Lecture, écriture"),
        (0x1, "Autorisations spéciales"),
    ],
)
def test_libelle_droits(masque, attendu):
    assert libelle_droits(masque) == attendu


def test_fusion_droit_explicite_et_herite():
    droits = fusionner([
        ("CLINIQUE\\DSI-Infra", LECTURE, False, True),
        ("CLINIQUE\\DSI-Infra", MODIFICATION, False, False),
        ("CLINIQUE\\Stagiaires", LECTURE, True, False),
    ])
    # Les refus d'abord, puis les droits cumulés (hérité + explicite = pas entièrement hérité)
    assert droits == [
        {"compte": "CLINIQUE\\Stagiaires", "droits": "Lecture", "refus": True, "herite": False},
        {"compte": "CLINIQUE\\DSI-Infra", "droits": "Modification", "refus": False, "herite": False},
    ]


@pytest.mark.skipif(sys.platform != "win32", reason="droits NTFS : Windows uniquement")
def test_lire_securite_fichier_reel(tmp_path):
    from ged.droits import lire_securite

    fichier = tmp_path / "note.txt"
    fichier.write_text("x")
    proprietaire, droits = lire_securite(fichier)
    assert proprietaire
    assert droits and all({"compte", "droits", "refus", "herite"} <= d.keys() for d in droits)


def _d(jour: int) -> datetime:
    return datetime(2026, 1, jour, tzinfo=timezone.utc)


def test_arbre_des_dossiers():
    racine = construire_arbre([
        ("Infra\\Reseau\\vpn.docx", _d(1)),
        ("Infra\\Reseau\\switch.pdf", _d(5)),
        ("Infra\\imprimantes.docx", _d(3)),
        ("lisez-moi.txt", _d(2)),
    ])
    assert racine.nb_documents == 4
    infra = trouver(racine, "Infra")
    assert infra.nb_documents == 3
    assert infra.derniere_modification == _d(5)
    assert [d.nom for d in infra.sous_dossiers()] == ["Reseau"]
    assert trouver(racine, "Infra\\Reseau").chemin == "Infra\\Reseau"
    assert trouver(racine, "Absent") is None


def test_chemins():
    assert dossier_parent("a\\b\\fichier.pdf") == "a\\b"
    assert dossier_parent("fichier.pdf") == ""
    assert fil_ariane("a\\b") == [("a", "a"), ("b", "a\\b")]
    assert fil_ariane("") == []


def test_filtre_dossier():
    assert Filtres(dossier="3:Infra\\Reseau\\").dossier_cible() == (3, "Infra\\Reseau")
    assert Filtres(dossier="3:").dossier_cible() == (3, "")
    assert Filtres(dossier="n'importe quoi").dossier_cible() is None
    assert "dossier=3%3AInfra" in Filtres(q="vpn", dossier="3:Infra").lien()


def test_arbre_des_resultats_fusionne_les_chaines_de_dossiers():
    from ged.arborescence import construire_arbre_resultats

    docs = [
        {"source_id": 1, "source": "GED Infra", "chemin_relatif": r"Infra\Reseau\VPN\vpn.docx", "nom": "vpn.docx"},
        {"source_id": 1, "source": "GED Infra", "chemin_relatif": r"Infra\Reseau\VPN\client.pdf", "nom": "client.pdf"},
        {"source_id": 1, "source": "GED Infra", "chemin_relatif": r"Infra\Imprimantes\spouleur.docx", "nom": "spouleur.docx"},
        {"source_id": 1, "source": "GED Infra", "chemin_relatif": "lisez-moi.txt", "nom": "lisez-moi.txt"},
    ]
    [source] = construire_arbre_resultats(docs)
    assert source["nb_documents"] == 4
    assert [f["nom"] for f in source["fichiers"]] == ["lisez-moi.txt"]
    [infra] = source["enfants"]
    assert infra.nom == "Infra" and infra.nb_documents == 3
    # "Reseau" ne contient que "VPN" : les deux sont fusionnés
    assert [e.nom for e in infra.enfants] == ["Imprimantes", "Reseau › VPN"]
    assert infra.enfants[1].chemin == r"Infra\Reseau\VPN"
    assert [f["nom"] for f in infra.enfants[1].fichiers] == ["client.pdf", "vpn.docx"]
