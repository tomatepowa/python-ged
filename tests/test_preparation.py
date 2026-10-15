"""Préparation de l'indexation : parcours rapide, exclusions, page « ged preparer » et « ged verifier »."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ged.config import Config, Source, charger, lire_exclusions
from ged.indexeur import dossier_exclu, parcourir
from ged.preparation import (
    alertes, ecrire_exclusions, exclusions_minimales, motif_exact, motifs_orphelins, parcourir_rapide,
    parcourir_sources,
)
from ged.verification import ATTENTION, ERREUR, OK, verifier_exclusions, verifier_sources
from ged.web.preparation import creer_app_preparation, niveau


@pytest.fixture
def partage(tmp_path: Path) -> Path:
    r"""partage\
         Notes\a.docx, b.pdf, Réseau\c.txt
         Applis\Logiciel [v2]\setup.exe, lisez-moi.txt
         #recycle\supprime.docx   (dossier système, jamais parcouru)
    """
    racine = tmp_path / "partage"
    for relatif, contenu in {
        "Notes/a.docx": "x" * 10,
        "Notes/b.pdf": "x" * 20,
        "Notes/Réseau/c.txt": "x" * 5,
        "Applis/Logiciel [v2]/setup.exe": "x",
        "Applis/Logiciel [v2]/lisez-moi.txt": "x",
        "#recycle/supprime.docx": "x",
    }.items():
        chemin = racine / relatif
        chemin.parent.mkdir(parents=True, exist_ok=True)
        chemin.write_text(contenu)
    return racine


def test_parcours_rapide_compte_comme_l_indexeur(partage):
    arbre = parcourir_rapide(partage)
    # Mêmes fichiers que l'indexation : ni programme ni dossier système
    assert arbre.fichiers == len(list(parcourir(partage, []))) == 4
    assert arbre.programmes == 1
    assert arbre.taille == 10 + 20 + 5 + 1
    noms = {d.nom: d for d in arbre.tous()}
    assert "#recycle" not in noms
    assert (noms["Notes"].fichiers, noms["Notes"].sous_dossiers) == (3, 1)
    assert arbre.sous_dossiers == 4  # Notes, Réseau, Applis, Logiciel [v2]


def test_identifiants_uniques_sur_toutes_les_sources(partage, tmp_path):
    autre = tmp_path / "autre"
    (autre / "x").mkdir(parents=True)
    cfg = Config(url_base="", sources=[Source("A", partage, "A"), Source("B", autre, "B")])
    ids = [d.id for _, arbre in parcourir_sources(cfg) for d in arbre.tous()]
    assert len(ids) == len(set(ids)) == 5 + 2


def test_exclusions_minimales_sans_les_sous_dossiers_deja_couverts():
    a, b = Path("/p/Applis"), Path("/p/Notes/Vieux")
    assert exclusions_minimales([a / "X", a, b, a / "X" / "Y"]) == [a, b]


def test_motif_exact_vise_seulement_ce_dossier(partage):
    logiciel = partage / "Applis" / "Logiciel [v2]"
    motif = motif_exact(logiciel)
    # Les crochets du nom seraient sinon lus comme « un caractère parmi v et 2 »
    assert dossier_exclu(logiciel, [motif])
    assert not dossier_exclu(partage / "Applis", [motif])
    assert not dossier_exclu(partage / "Applis" / "Logiciel v", [motif])


def test_fichier_d_exclusions_relu_par_la_configuration(partage, tmp_path):
    arbre = parcourir_rapide(partage)
    noms = {d.nom: d for d in arbre.tous()}
    fichier = tmp_path / "exclusions.toml"
    ecrire_exclusions(fichier, [noms["Logiciel [v2]"]], conserves=["\\\\ancien\\partage\\d'Antoine"])
    motifs = lire_exclusions(fichier)
    assert motifs == [motif_exact(noms["Logiciel [v2]"].chemin), "\\\\ancien\\partage\\d'Antoine"]
    # L'indexeur ne parcourt plus le dossier exclu
    assert {p.name for p in parcourir(partage, motifs)} == {"a.docx", "b.pdf", "c.txt"}

    (tmp_path / "config.toml").write_text(
        '[base]\nurl = "postgresql://x"\n[indexation]\nexclure = ["*\\\\Archives\\\\*"]\n', encoding="utf-8"
    )
    cfg = charger(tmp_path / "config.toml")
    assert cfg.exclure_config == ["*\\Archives\\*"]
    assert cfg.exclure == ["*\\Archives\\*", *motifs]


def test_fichier_d_exclusions_invalide(tmp_path):
    fichier = tmp_path / "exclusions.toml"
    fichier.write_text("exclure = [1, 2]\n")
    with pytest.raises(SystemExit, match="liste de chemins"):
        lire_exclusions(fichier)


def test_motifs_orphelins(partage):
    sources = [("P", parcourir_rapide(partage))]
    assert motifs_orphelins([motif_exact(partage / "Notes"), "\\\\ailleurs\\x"], sources) == ["\\\\ailleurs\\x"]


def test_alertes(partage):
    noms = {d.nom: d for d in parcourir_rapide(partage).tous()}
    assert alertes(noms["Notes"]) == {}
    vieux = noms["Notes"]
    vieux.recent = time.time() - 7 * 365.25 * 24 * 3600
    assert alertes(vieux) == {"ancien": "rien de modifié depuis 7 ans"}
    noms["Applis"].nom = "Logiciels_Install"
    assert "nom" in alertes(noms["Applis"])
    noms["Applis"].nom = "Templates"  # « temp » au début ne suffit pas
    assert "nom" not in alertes(noms["Applis"])


def test_niveau_de_couleur():
    assert [niveau(n, 1000) for n in (0, 9, 10, 50, 199, 200)] == [0, 0, 1, 2, 2, 3]
    assert niveau(5, 0) == 0


@pytest.fixture
def preparation(partage, tmp_path):
    cfg = Config(
        url_base="", sources=[Source("Partage", partage, "P")],
        exclure=["*\\Notes\\Réseau"], exclure_config=["*\\Notes\\Réseau"],
        fichier_exclusions=tmp_path / "exclusions.toml",
    )
    sources = parcourir_sources(cfg)
    return cfg, sources, TestClient(creer_app_preparation(cfg, sources))


def test_page_de_preparation(preparation):
    _, sources, client = preparation
    page = client.get("/")
    assert page.status_code == 200
    assert "Logiciel [v2]" in page.text and "#recycle" not in page.text
    # Dossier exclu par config.toml : décoché, et verrouillé
    reseau = next(d for d in sources[0][1].tous() if d.nom == "Réseau")
    case = re.search(rf'<input[^>]*value="{reseau.id}"[^>]*>', page.text).group(0)
    assert "checked" not in case and "disabled" in case


def test_validation_ecrit_les_exclusions(preparation):
    cfg, sources, client = preparation
    page = client.get("/").text
    jeton = re.search(r'name="jeton" value="([^"]+)"', page).group(1)
    noms = {d.nom: d for d in sources[0][1].tous()}
    # Applis décoché (et donc son sous-dossier), Réseau verrouillé : tout le reste gardé
    gardes = [d.id for d in noms.values() if d.nom not in ("Applis", "Logiciel [v2]", "Réseau")]
    corps = "&".join([f"jeton={jeton}", *(f"garder={i}" for i in gardes)])
    reponse = client.post("/valider", content=corps, headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert reponse.status_code == 200
    # Seul le dossier le plus haut est écrit ; Réseau reste dans config.toml
    assert lire_exclusions(cfg.fichier_exclusions) == [motif_exact(noms["Applis"].chemin)]
    # Applis (1 fichier compté : le programme ne l'est pas) et Réseau (1, exclu par config.toml) écartés
    assert "<strong>2 fichiers à indexer</strong> sur 4" in reponse.text
    # La page affiche ensuite le nouvel état
    applis = re.search(rf'<input[^>]*value="{noms["Applis"].id}"[^>]*>', client.get("/").text).group(0)
    assert "checked" not in applis


def test_validation_refusee_sans_jeton(preparation):
    cfg, _, client = preparation
    reponse = client.post("/valider", content="garder=1", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert reponse.status_code == 403
    assert not cfg.fichier_exclusions.exists()


def test_verifier_sources(partage, tmp_path):
    cfg = Config(url_base="", sources=[
        Source("Partage", partage, "P"), Source("Notes", partage / "Notes", "N"), Source("Absente", tmp_path / "x", "X"),
    ])
    statuts = {(v.sujet, v.statut) for v in verifier_sources(cfg)}
    assert ("Source « Partage »", OK) in statuts
    assert ("Source « Absente »", ERREUR) in statuts
    assert ("Sources", ATTENTION) in statuts  # Notes est dans Partage


def test_verifier_exclusions(tmp_path):
    cfg = Config(url_base="", sources=[], fichier_exclusions=tmp_path / "exclusions.toml")
    assert verifier_exclusions(cfg).statut == ATTENTION
    (tmp_path / "exclusions.toml").write_text("exclure = []\n")
    assert verifier_exclusions(cfg).statut == OK


@pytest.mark.skipif(os.name != "nt", reason="chemins Windows")
def test_parcours_signale_un_dossier_inaccessible(tmp_path):
    arbre = parcourir_rapide(tmp_path / "inexistant")
    assert arbre.inaccessible and arbre.fichiers == 0


def test_alerte_du_parent_non_repetee(partage, tmp_path):
    vieux = time.time() - 9 * 365.25 * 24 * 3600
    for fichier in (partage / "Notes").rglob("*"):
        os.utime(fichier, (vieux, vieux))
    cfg = Config(url_base="", sources=[Source("P", partage, "P")], fichier_exclusions=tmp_path / "e.toml")
    page = TestClient(creer_app_preparation(cfg, parcourir_sources(cfg))).get("/").text
    # Notes et son sous-dossier Réseau sont anciens : l'alerte n'apparaît qu'une fois, sur Notes
    assert page.count("rien de modifié depuis 9 ans") == 1
    # La racine de la source n'a pas de case à cocher
    assert page.count('name="garder"') == 4


def test_resume_proprietaires():
    from ged.verification import resume_proprietaires

    ok = resume_proprietaires("P", ["DOM\\a", "DOM\\a", "DOM\\b"], 0.03)
    assert ok.statut == OK and "2 propriétaires dont DOM\\a (2), DOM\\b (1)" in ok.detail and "10 ms/fichier" in ok.detail
    illisibles = resume_proprietaires("P", ["DOM\\a", None], 0.01)
    assert illisibles.statut == ATTENTION and "1 fichiers aux droits illisibles" in illisibles.detail
    assert resume_proprietaires("P", ["S-1-5-21-1-2-3-500"], 0.001).statut == ATTENTION
    assert resume_proprietaires("P", ["DOM\\a"], 0.2).statut == ATTENTION  # 200 ms par fichier
    assert resume_proprietaires("P", [], 0).statut == ATTENTION


def test_resume_groupes():
    from ged.verification import resume_groupes

    noms = {"S-1-5-21-1-2-3-513": "DOM\\Utilisateurs du domaine", "S-1-1-0": "Tout le monde"}
    assert resume_groupes("DOM\\moi", noms, 0.1, {"S-1-5-21-1-2-3-513": []}, 0.2).statut == OK
    assert "hors domaine" in resume_groupes("POSTE\\moi", noms, 0.1, None, 0).detail
    lent = resume_groupes("DOM\\moi", noms, 1.5, {}, 1.0)
    assert lent.statut == ATTENTION and "lent" in lent.detail
    sans_nom = resume_groupes("DOM\\moi", {"S-1-5-21-9-9-9-1105": "S-1-5-21-9-9-9-1105"}, 0.1, {}, 0.1)
    assert "1 groupes sans nom" in sans_nom.detail
