import sys
import pytest

from ged.arborescence import dossier_parent, fil_ariane
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


def test_vue_arborescence_par_defaut():
    # L'arborescence est la vue par défaut : elle n'apparaît pas dans l'URL, la liste si
    assert Filtres(q="vpn").lien() == "q=vpn"
    assert Filtres(q="vpn", vue="liste").lien() == "q=vpn&vue=liste"
    assert Filtres(q="vpn", page=2).lien() == "q=vpn&page=2"


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


def test_decoupage_du_chemin_affiche():
    from ged.recherche import decouper_chemin

    niveaux = fil_ariane(r"a\b\c\d\e\f\g")
    decoupe = decouper_chemin(niveaux)
    assert [n for n, _ in decoupe["debut"]] == ["a", "b", "c", "d"]
    assert [n for n, _ in decoupe["milieu"]] == ["e", "f"]
    assert decoupe["fin"] == [("g", r"a\b\c\d\e\f\g")]
    # Chemin court : tout dans le début, rien à réduire
    assert decouper_chemin(fil_ariane(r"a\b\c\d\e")) == {"debut": fil_ariane(r"a\b\c\d\e"), "milieu": [], "fin": []}
