"""Taxonomie en base : import et export TOML, aperçu, recalcul d'un tag, recalcul en arrière-plan."""

from __future__ import annotations

import threading
import time

import pytest

from ged import tags_manuels, taxonomie_base
from ged.recalcul_tags import RecalculEnArrierePlan, RecalculEnCours, verrou_recalcul
from ged.taxonomie import Definition, Tag, Taxonomie, en_toml, lire_toml
from tests.test_tags_manuels import ADMIN, ALICE, creer_documents, tags_de

TOML = """
seuil = 2
densite_min = 0.5

[logiciel]
"Veeam" = ["veeam", "titre:sauvegarde"]
VPN = ["vpn", "forticlient"]

[type]
"Procédure" = ["titre:procedure"]
"Note \\"technique\\"" = []
"""


# --- Sans base ------------------------------------------------------------------------------


def test_aller_retour_toml():
    definition = lire_toml(TOML)
    assert definition.mots[Tag("logiciel", "Veeam")] == ["veeam", "titre:sauvegarde"]
    assert definition.mots[Tag("type", 'Note "technique"')] == []
    texte = en_toml(definition)
    assert texte.startswith("# Taxonomie")
    assert lire_toml(texte) == definition
    # Catégories puis tags triés, sans tenir compte des accents
    assert texte.index("[logiciel]") < texte.index("[type]")
    assert texte.index('"Note \\"technique\\""') < texte.index('"Procédure"')


def test_toml_invalide():
    with pytest.raises(ValueError, match="liste de mots-clés"):
        lire_toml('[logiciel]\nVeeam = "veeam"\n')
    with pytest.raises(ValueError, match="TOML invalide"):
        lire_toml("[logiciel\n")


def test_taxonomie_du_projet_s_exporte_a_l_identique():
    from tests.test_taxonomie import RACINE

    definition = lire_toml((RACINE / "taxonomie.toml").read_text(encoding="utf-8"))
    assert lire_toml(en_toml(definition)) == definition


def test_nettoyage_des_mots_cles():
    assert taxonomie_base.nettoyer_mots(["  Veeam ", "veeam", "Titre : Sauvegarde", "", "titre:", "boite  mail"]) == [
        "veeam", "titre:sauvegarde", "boite mail",
    ]
    # Doublons à l'accent près
    assert taxonomie_base.nettoyer_mots(["identité", "identite"]) == ["identité"]
    with pytest.raises(tags_manuels.TagInvalide, match="trop long"):
        taxonomie_base.nettoyer_mots(["x" * 200])


def test_expliquer():
    t = Taxonomie.construire(Definition({Tag("logiciel", "VPN"): ["vpn", "titre:fortinet"]}, seuil=1))
    assert t.expliquer(Tag("logiciel", "VPN"), r"Infra\Fortinet_install.docx", "") == "« fortinet » dans le nom"
    assert t.expliquer(Tag("logiciel", "VPN"), "note.docx", "VPN, encore le VPN") == "2 mentions dans le texte (« vpn »)"


def test_file_des_recalculs():
    """Un recalcul complet en attente couvre les recalculs de tags ; un tag n'est demandé qu'une fois."""
    depart, faits = threading.Event(), []

    class Connexion:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def executer(_conn, tag_id, avancement):
        depart.wait(5)
        avancement(1, 1)
        faits.append(tag_id)
        if tag_id == 3:
            raise RuntimeError("base injoignable")
        return "ok"

    file = RecalculEnArrierePlan(Connexion, executer)
    file.demander(1, "« A »")            # commence aussitôt, bloqué sur `depart`
    while not file.etat()["en_cours"]:
        time.sleep(0.01)
    file.demander(2, "« B »")
    file.demander(2, "« B »")
    assert file.etat()["en_attente"] == ["« B »"]
    file.demander(None, "tous les tags")
    file.demander(2, "« B »")            # couvert par le recalcul complet
    assert file.etat()["en_attente"] == ["tous les tags"]
    depart.set()
    file.attendre()
    assert faits == [1, None]
    assert file.etat() | {"en_attente": []} == {
        "en_cours": False, "libelle": "tous les tags", "fait": 1, "total": 1,
        "message": "Recalcul de tous les tags terminé : ok", "erreur": "", "en_attente": [],
    }
    file.demander(3, "« C »")
    file.attendre()
    assert "base injoignable" in file.etat()["erreur"]


# --- Sur la base de test (fixture conn) -----------------------------------------------------


def test_import_export_et_premier_demarrage(conn, tmp_path):
    fichier = tmp_path / "taxonomie.toml"
    fichier.write_text(TOML, encoding="utf-8")
    assert taxonomie_base.importer_au_premier_demarrage(conn, fichier)
    # Une seule fois, même si les mots-clés sont vidés ensuite
    assert not taxonomie_base.importer_au_premier_demarrage(conn, fichier)
    assert taxonomie_base.definition(conn) == lire_toml(TOML)
    assert lire_toml(taxonomie_base.exporter(conn)) == lire_toml(TOML)
    assert Tag("logiciel", "VPN") in taxonomie_base.charger(conn).regles
    assert Tag("type", 'Note "technique"') not in taxonomie_base.charger(conn).regles   # sans mot-clé


def test_import_remplace_et_garde_les_tags(conn):
    taxonomie_base.importer(conn, lire_toml(TOML), "test")
    manuel = tags_manuels.creer(conn, "logiciel", "Citrix", ADMIN)
    doc = creer_documents(conn, "a.txt")["a.txt"]
    tags_manuels.ajouter(conn, doc, manuel, ALICE)
    nouvelle = lire_toml('seuil = 3\n[logiciel]\nveeam = ["veeam", "b&r"]\n')
    differences = taxonomie_base.differences(conn, nouvelle)
    assert "réglages : seuil 2 → 3, densité 0.5 → 0.5" in differences
    assert "~ veeam (logiciel) : +b&r, −titre:sauvegarde" in differences   # retrouvé malgré la casse
    assert any(ligne.startswith("- VPN (logiciel)") for ligne in differences)
    taxonomie_base.importer(conn, nouvelle, "import de x.toml")
    definition = taxonomie_base.definition(conn)
    assert definition.seuil == 3
    assert definition.mots[Tag("logiciel", "Veeam")] == ["veeam", "b&r"]
    assert definition.mots[Tag("logiciel", "VPN")] == []
    # Les tags absents du fichier restent, avec leurs poses à la main
    assert tags_de(conn, doc) == {"Citrix": "manuel"}
    assert taxonomie_base.differences(conn, nouvelle) == []
    assert {"mots-clés", "réglages"} <= {j["action"] for j in tags_manuels.journal(conn, par="import de x.toml")}


def test_apercu_puis_recalcul_d_un_tag(conn, tmp_path):
    from tests.test_tags_manuels import indexeur

    docs = creer_documents(conn, r"Infra\Sauvegarde Veeam.docx", r"Infra\note.txt", r"Infra\vpn.txt", r"Infra\autre.txt")
    conn.execute("UPDATE document SET contenu = 'Le job Veeam échoue. Relancer le job Veeam.' WHERE id = %s",
                 (docs[r"Infra\note.txt"],))
    idx = indexeur(conn, tmp_path, '[logiciel]\nVeeam = ["veeam"]\nVPN = ["vpn"]\n')
    for chemin, doc in docs.items():
        idx._appliquer_tags(doc, chemin, conn.execute("SELECT contenu FROM document WHERE id = %s", (doc,)).fetchone()["contenu"] or "")
    veeam = tags_manuels.id_tag(conn, "logiciel", "Veeam")
    assert {d for d in docs.values() if "Veeam" in tags_de(conn, d)} == {docs[r"Infra\Sauvegarde Veeam.docx"], docs[r"Infra\note.txt"]}
    # Un administrateur l'a retiré de la note : ni l'aperçu ni le recalcul ne le remettent
    tags_manuels.retirer(conn, docs[r"Infra\note.txt"], veeam, ADMIN, idx.taxonomie)

    apercu = taxonomie_base.apercu(conn, veeam, ["titre:veeam", "vpn", "de"])
    assert apercu["total"] == 2
    assert apercu["parcours_complet"]            # « de » : mot vide, pas de pré-filtre possible
    assert (apercu["nb_gagnes"], apercu["nb_perdus"], apercu["nb_retires"]) == (1, 0, 0)
    assert apercu["gagnes"] == [{"id": docs[r"Infra\vpn.txt"], "chemin": r"Infra\vpn.txt", "raison": "« vpn » dans le nom"}]
    assert {m["mot"]: m["nb"] for m in apercu["mots"]} == {"titre:veeam": 1, "vpn": 1, "de": 0}
    assert "préférez « titre:vpn »" in apercu["mots"][1]["alerte"]

    taxonomie_base.modifier_mots(conn, veeam, ["vpn"], ADMIN)
    assert taxonomie_base.recalculer_tag(conn, veeam) == {"examines": 2, "poses": 1, "retraits": 1}
    assert {d for d in docs.values() if "Veeam" in tags_de(conn, d)} == {docs[r"Infra\vpn.txt"]}
    # Plus de mots-clés : plus aucune pose par la taxonomie
    taxonomie_base.modifier_mots(conn, veeam, [], ADMIN)
    assert taxonomie_base.recalculer_tag(conn, veeam)["retraits"] == 1
    assert [j["detail"] for j in tags_manuels.journal(conn, tag_id=veeam) if j["action"] == "mots-clés"][:2] == [
        "−vpn", "+vpn, −veeam",
    ]


def test_reglages_et_recalcul_complet(conn, tmp_path):
    from tests.test_tags_manuels import indexeur

    doc = creer_documents(conn, "note.txt")["note.txt"]
    conn.execute("UPDATE document SET contenu = 'Le vpn est coupé.' WHERE id = %s", (doc,))
    indexeur(conn, tmp_path, '[logiciel]\nVPN = ["vpn"]\n')
    assert taxonomie_base.recalculer_tous(conn)["examines"] == 1
    assert tags_de(conn, doc) == {}                           # une mention, seuil 2
    assert taxonomie_base.modifier_reglages(conn, 1, 0.5, ADMIN)
    assert not taxonomie_base.modifier_reglages(conn, 1, 0.5, ADMIN)
    with pytest.raises(tags_manuels.TagInvalide):
        taxonomie_base.modifier_reglages(conn, 0, 0.5, ADMIN)
    avancement = []
    taxonomie_base.recalculer_tous(conn, lambda fait, total: avancement.append((fait, total)))
    assert tags_de(conn, doc) == {"VPN": "auto"}
    assert avancement == [(1, 1)]


def test_fusion_reporte_les_mots_cles(conn):
    taxonomie_base.importer(conn, lire_toml('[logiciel]\nVeeam = ["veeam", "b&r"]\n"Veeam B&R" = ["B&R", "veeam backup"]\n'), "test")
    veeam, doublon = (tags_manuels.id_tag(conn, "logiciel", libelle) for libelle in ("Veeam", "Veeam B&R"))
    tags_manuels.fusionner(conn, doublon, veeam, ADMIN)
    assert taxonomie_base.definition(conn).mots == {Tag("logiciel", "Veeam"): ["veeam", "b&r", "veeam backup"]}


def test_renommer_un_tag_de_la_taxonomie(conn):
    taxonomie_base.importer(conn, lire_toml('[logiciel]\nVeeam = ["veeam"]\n'), "test")
    veeam = tags_manuels.id_tag(conn, "logiciel", "Veeam")
    tags_manuels.renommer(conn, veeam, "logiciel", "Veeam Backup", ADMIN)
    assert taxonomie_base.charger(conn).regles.keys() == {Tag("logiciel", "Veeam Backup")}


def test_verrou_des_recalculs(conn):
    import os

    import psycopg
    from psycopg.rows import dict_row

    with (verrou_recalcul(conn), psycopg.connect(os.environ["GED_TEST_BASE"], row_factory=dict_row) as autre,
          pytest.raises(RecalculEnCours), verrou_recalcul(autre)):
        pass
    with psycopg.connect(os.environ["GED_TEST_BASE"], row_factory=dict_row) as autre, verrou_recalcul(autre):
        pass


def test_page_tags(conn):
    """Page Tags sur la base de test : affichage, aperçu, enregistrement puis recalcul, export."""
    import os

    from fastapi.testclient import TestClient

    from ged.config import Config
    from ged.web.app import creer_app
    from tests.test_tags_manuels import FORMULAIRE

    doc = creer_documents(conn, r"Infra\Procédure Veeam.docx")[r"Infra\Procédure Veeam.docx"]
    taxonomie_base.importer(conn, lire_toml('[logiciel]\nVeeam = ["veeam"]\n'), "test")
    veeam = tags_manuels.id_tag(conn, "logiciel", "Veeam")
    assert taxonomie_base.recalculer_tag(conn, veeam)["poses"] == 1
    conn.commit()   # visible des connexions de l'application (la fixture revide la base)
    app = creer_app(Config(url_base=os.environ["GED_TEST_BASE"], sources=[], authentification=False))
    entetes = {**FORMULAIRE, "Origin": "http://vm-ged:8000"}
    with TestClient(app, base_url="http://vm-ged:8000") as client:
        page = client.get("/admin/tags").text
        assert "<code>veeam</code>" in page and "Exporter taxonomie.toml" in page
        apercu = client.post("/admin/tags/apercu", content=f"tag_id={veeam}&mots=titre%3Aprocedure", headers=entetes)
        assert "porterait ce tag" in apercu.text and r"Infra\Procédure Veeam.docx" not in apercu.text   # déjà posé
        reponse = client.post("/admin/tags", content=f"action=mots_cles&tag_id={veeam}&mots=vpn", headers=entetes,
                              follow_redirects=False)
        assert reponse.status_code == 303 and "recalcul" in reponse.headers["location"]
        app.state.recalcul.attendre()
        etat = client.get("/admin/tags/recalcul").json()
        assert etat["erreur"] == "" and "retiré de 1" in etat["message"]
        assert tags_de(conn, doc) == {}
        export = client.get("/admin/taxonomie.toml")
        assert "attachment" in export.headers["content-disposition"]
        assert lire_toml(export.text).mots == {Tag("logiciel", "Veeam"): ["vpn"]}
