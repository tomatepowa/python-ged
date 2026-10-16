"""Réglages : connexion.toml, formulaire, assistant d'installation et page Réglages."""

from __future__ import annotations

import os
from urllib.parse import parse_qs, urlencode

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict

from ged import config as configuration
from ged import parametres
from ged.config import Connexion, NonConfiguree, ecrire_connexion, lire_connexion
from ged.web.assistant import ESSAIS_MAX, creer_app_assistant, generer_code

LOCAL = ("127.0.0.1", 50000)
FORMULAIRE = {"Content-Type": "application/x-www-form-urlencoded", "Origin": "http://testserver"}


# --- connexion.toml ---------------------------------------------------------------------------


def test_connexion_ecrite_puis_relue(tmp_path):
    fichier = tmp_path / "sous" / "connexion.toml"
    assert lire_connexion(fichier) is None
    url = 'postgresql://ged:mot"de\\passe@localhost:5432/ged'
    ecrire_connexion(Connexion(url, "0.0.0.0", 8080), fichier)
    assert lire_connexion(fichier) == Connexion(url, "0.0.0.0", 8080)


def test_variable_d_environnement(tmp_path, monkeypatch):
    monkeypatch.setenv("GED_CONNEXION", str(tmp_path / "ailleurs.toml"))
    assert configuration.fichier_connexion() == tmp_path / "ailleurs.toml"
    assert configuration.fichier_connexion(tmp_path / "x.toml") == tmp_path / "x.toml"


def test_pas_encore_installee(tmp_path):
    with pytest.raises(NonConfiguree, match="ged serveur"):
        configuration.charger(tmp_path / "absent.toml")


def test_connexion_illisible(tmp_path):
    fichier = tmp_path / "connexion.toml"
    fichier.write_text("[base]\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="--assistant"):
        lire_connexion(fichier)


# --- Formulaire ---------------------------------------------------------------------------------


def champs(**valeurs) -> dict[str, list[str]]:
    """Champs d'un formulaire valide, modifiés par `valeurs` (une liste pour les champs répétés)."""
    base = {
        "source_nom": ["Infra", "", "Métier"], "source_chemin": ["C:\\", "", "C:\\inexistant\\x"],
        "source_pole": ["", "", "Métier"], "source_depot": ["2"],
        "taille_max_mo": "100", "exclure_manuels": "*\\Archives\\*\n\n  *\\Temp\\*  ",
        "ocr_active": "", "tessdata": "", "langue_ocr": "fra", "dpi_ocr": "300", "soffice": "",
        "authentification": "1", "groupes_admin": "DOM\\GED-Admins\nDOM\\dsi", "protocole": "NTLM",
        "duree_session_heures": "8,5", "attente_depot": "depot", "taille_max_depot_mo": "50",
        "exemples_recherche": "imprimante", "ecoute": "reseau", "hote_autre": "", "port": "8080",
    }
    base.update(valeurs)
    return {cle: v if isinstance(v, list) else [v] for cle, v in base.items()}


def test_formulaire_valide():
    actuelles = {**parametres.DEFAUTS, "exclure_prepares": ["\\\\nas\\x\\Applis"]}
    f = parametres.lire_formulaire(champs(), actuelles)
    assert f.erreurs == []
    v = f.valeurs
    # Ligne vide ignorée, case « Dépôt » rattachée à sa ligne, pôle vide : le nom
    assert v["sources"] == [
        {"nom": "Infra", "chemin": "C:\\", "pole": "Infra", "depot": False},
        {"nom": "Métier", "chemin": "C:\\inexistant\\x", "pole": "Métier", "depot": True},
    ]
    assert any("Métier" in a and "inaccessible" in a for a in f.avertissements)
    assert v["exclure_manuels"] == ["*\\Archives\\*", "*\\Temp\\*"]
    assert v["exclure_prepares"] == ["\\\\nas\\x\\Applis"]  # pas dans le formulaire : gardé
    assert v["groupes_admin"] == ["DOM\\GED-Admins", "DOM\\dsi"]
    assert v["duree_session_heures"] == 8.5
    assert (f.hote, f.port) == ("0.0.0.0", 8080)
    cfg = parametres.config(Connexion("postgresql://x", f.hote, f.port), v)
    assert cfg.sources_depot == ["Métier"] and cfg.port == 8080


@pytest.mark.parametrize(("modifs", "erreur"), [
    ({"groupes_admin": ""}, "administrateur"),
    ({"port": "99999"}, "Port"),
    ({"taille_max_mo": "beaucoup"}, "Taille maximale lue"),
    ({"source_nom": ["Infra", "Infra", ""], "source_chemin": ["C:\\", "D:\\", ""]}, "Deux sources"),
    ({"source_nom": ["", "", "Métier"], "source_chemin": ["C:\\", "", "C:\\x"]}, "obligatoires"),
    ({"ocr_active": "1", "tessdata": ""}, "OCR"),
    ({"protocole": "Basic"}, "Protocole"),
    ({"duree_session_heures": "toujours"}, "Durée"),
    ({"ecoute": "autre", "hote_autre": ""}, "adresse d'écoute"),
])
def test_formulaire_refuse(modifs, erreur):
    f = parametres.lire_formulaire(champs(**modifs), parametres.DEFAUTS)
    assert any(erreur in e for e in f.erreurs), f.erreurs


def test_sans_identification_pas_besoin_d_administrateur():
    assert parametres.lire_formulaire(champs(authentification="", groupes_admin=""), parametres.DEFAUTS).erreurs == []


# --- Assistant d'installation (sans base) ----------------------------------------------------------


def test_code_lisible():
    code = generer_code()
    assert len(code) == 9 and code[4] == "-" and not set(code) & set("01OIL")


def assistant(tmp_path, terminer=lambda: None):
    return creer_app_assistant(tmp_path / "connexion.toml", terminer, code="ABCD-EFGH",
                               taxonomie=tmp_path / "taxonomie.toml")


def test_assistant_ce_poste_seulement(tmp_path):
    reponse = TestClient(assistant(tmp_path), client=("10.0.0.5", 50000)).get("/assistant")
    assert reponse.status_code == 403


def test_assistant_redirige_tout_vers_l_installation(tmp_path):
    client = TestClient(assistant(tmp_path), client=LOCAL)
    reponse = client.get("/document/1", follow_redirects=False)
    assert reponse.headers["location"] == "/assistant"
    assert "Code d'installation" in client.get("/assistant").text
    # Réglages inaccessibles sans le code
    assert client.get("/assistant/reglages", follow_redirects=False).headers["location"] == "/assistant"


def test_assistant_mauvais_code(tmp_path):
    app = assistant(tmp_path)
    client = TestClient(app, client=LOCAL)
    corps = urlencode({"code": "ZZZZ-ZZZZ", "hote_base": "127.0.0.1", "port_base": "1", "base": "ged",
                       "utilisateur": "ged", "mot_de_passe": "x"})
    reponse = client.post("/assistant/connexion", content=corps, headers=FORMULAIRE)
    assert reponse.status_code == 403 and "Code incorrect" in reponse.text
    for _ in range(ESSAIS_MAX):
        client.post("/assistant/connexion", content=corps, headers=FORMULAIRE)
    # Bloqué, même avec le bon code
    reponse = client.post("/assistant/connexion", content=corps.replace("ZZZZ-ZZZZ", "ABCD-EFGH"), headers=FORMULAIRE)
    assert reponse.status_code == 403 and "Trop d" in reponse.text
    assert not (tmp_path / "connexion.toml").exists()


def test_assistant_base_injoignable(tmp_path):
    client = TestClient(assistant(tmp_path), client=LOCAL)
    corps = urlencode({"code": "abcd-efgh", "hote_base": "127.0.0.1", "port_base": "1", "base": "ged",
                       "utilisateur": "ged", "mot_de_passe": "x"})
    reponse = client.post("/assistant/connexion", content=corps, headers=FORMULAIRE)
    assert reponse.status_code == 400 and "Connexion impossible" in reponse.text


# --- Sur une base PostgreSQL de test (GED_TEST_BASE, fixture conn) ----------------------------


def corps_reglages(**valeurs) -> str:
    return urlencode([(cle, v) for cle, liste in champs(**valeurs).items() for v in liste])


def test_installation_complete(tmp_path, conn, monkeypatch):
    conn.execute("DELETE FROM parametre")
    conn.commit()
    (tmp_path / "taxonomie.toml").write_text('[logiciel]\nVeeam = ["veeam"]\n', encoding="utf-8")
    termine = []
    client = TestClient(assistant(tmp_path, lambda: termine.append(True)), client=LOCAL)

    base = conninfo_to_dict(os.environ["GED_TEST_BASE"])
    corps = urlencode({"code": "abcd-efgh", "hote_base": base["host"], "port_base": base.get("port", "5432"),
                       "base": base["dbname"], "utilisateur": base["user"], "mot_de_passe": base.get("password", "")})
    reponse = client.post("/assistant/connexion", content=corps, headers=FORMULAIRE, follow_redirects=False)
    assert reponse.headers["location"] == "/assistant/reglages", reponse.text
    page = client.get("/assistant/reglages").text
    assert "Importer la taxonomie" in page

    # Erreur : page réaffichée avec la saisie, rien d'enregistré
    reponse = client.post("/assistant/reglages", content=corps_reglages(groupes_admin=""), headers=FORMULAIRE)
    assert reponse.status_code == 400 and "imprimante" in reponse.text
    assert not termine

    reponse = client.post("/assistant/reglages", content=corps_reglages() + "&importer_taxonomie=1", headers=FORMULAIRE)
    assert reponse.status_code == 200 and "Installation terminée" in reponse.text
    assert termine == [True]
    cfg = configuration.charger(tmp_path / "connexion.toml")
    assert [s.nom for s in cfg.sources] == ["Infra", "Métier"] and cfg.sources_depot == ["Métier"]
    assert (cfg.hote, cfg.port, cfg.groupes_admin) == ("0.0.0.0", 8080, ["DOM\\GED-Admins", "DOM\\dsi"])
    assert conn.execute("SELECT count(*) AS nb FROM tag WHERE libelle = 'Veeam'").fetchone()["nb"] == 1


def test_page_reglages(tmp_path, conn):
    from ged.web.app import creer_app

    conn.execute("DELETE FROM parametre")
    conn.commit()
    fichier = tmp_path / "connexion.toml"
    connexion = Connexion(os.environ["GED_TEST_BASE"], "127.0.0.1", 8000)
    ecrire_connexion(connexion, fichier)
    parametres.ecrire(conn, {**parametres.DEFAUTS, "authentification": False}, "test", installation=True)
    cfg = configuration.charger(fichier)

    with TestClient(creer_app(cfg, fichier_connexion=fichier), base_url="http://vm-ged:8000") as client:
        origine = {**FORMULAIRE, "Origin": "http://vm-ged:8000"}
        assert "Sources à indexer" in client.get("/admin/reglages").text
        reponse = client.post("/admin/reglages", content=corps_reglages(port="abc"), headers=origine)
        assert reponse.status_code == 400 and "Port" in reponse.text

        reponse = client.post("/admin/reglages", content=corps_reglages(exemples_recherche="sauvegarde Veeam"),
                              headers=origine, follow_redirects=False)
        assert reponse.status_code == 303
        avertissements = parse_qs(reponse.headers["location"].split("?", 1)[1])["avertissement"]
        # Identification, administrateurs, adresse d'écoute : au prochain démarrage
        assert any("prochain démarrage" in a and "adresse d'écoute" in a and "administrateurs" in a
                   for a in avertissements)
        # Le reste tout de suite : l'exemple de la barre de recherche a changé
        assert "sauvegarde Veeam" in client.get("/").text
        assert lire_connexion(fichier) == Connexion(connexion.url, "0.0.0.0", 8080)
        # Réglage refusé depuis une autre page
        reponse = client.post("/admin/reglages", content=corps_reglages(), headers={**FORMULAIRE, "Origin": "http://autre"})
        assert reponse.status_code == 403


# --- Aide des commandes du serveur ------------------------------------------------------------------


def test_aide_des_commandes_complete():
    from ged.commandes import aide, construire_parser

    contenu = aide()
    noms = [c["nom"] for c in contenu["commandes"]]
    assert {"serveur", "indexer", "ocr", "preparer", "verifier", "reinitialiser"} <= set(noms)
    # Chaque commande a son but et sa description ; les options viennent de la vraie définition
    assert all(c["but"] and c["detail"] for c in contenu["commandes"])
    ocr = next(c for c in contenu["commandes"] if c["nom"] == "ocr")
    assert [o["noms"] for o in ocr["options"]] == ["--limite", "--reessayer"]
    assert any(o["noms"] == "-c, --connexion" for o in contenu["options"])
    assert construire_parser().parse_args(["ocr", "--reessayer"]).reessayer


def test_page_des_commandes():
    from tests.test_authentification import NegociateurFactice, ntlm
    from ged.config import Config
    from ged.web.app import creer_app

    base = "postgresql://ged@127.0.0.1:1/ged"
    page = TestClient(creer_app(Config(url_base=base, sources=[], authentification=False))).get("/admin/commandes")
    assert page.status_code == 200
    assert "ged indexer" in page.text and "--reessayer" in page.text

    client = TestClient(creer_app(Config(url_base=base, sources=[]), NegociateurFactice()))
    client.get("/connexion", headers=ntlm(b"reponse"), follow_redirects=False)
    assert client.get("/admin/commandes").status_code == 403
