"""Dépôt de nouveaux documents : noms acceptés, contrôles d'accès, analyse, validation et rejet."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ged import depot, parametres
from ged.acces import Lecteur
from ged.config import Config, Connexion, Source
from ged.web.app import creer_app

ADMIN = Lecteur("DOM\\dsi", frozenset(), frozenset(), admin=True, sid="S-1-5-21-1-2-3-500")
ALICE = Lecteur("DOM\\alice", frozenset(), frozenset(), sid="S-1-5-21-1-2-3-1101")
BOB = Lecteur("DOM\\bob", frozenset(), frozenset(), sid="S-1-5-21-1-2-3-1102")
TAXONOMIE = '[logiciel]\nVeeam = ["veeam"]\nGLPI = ["glpi"]\n'


# --- Noms de fichiers et de dossiers ----------------------------------------------------------


@pytest.mark.parametrize("nom", ["", "  ", "a<b.docx", "a:b.pdf", "a?.txt", "CON.txt", "nul", "..", "x" * 151 + ".pdf"])
def test_noms_refuses(nom):
    with pytest.raises(depot.DepotRefuse):
        depot.nom_valide(nom)


def test_noms_acceptes():
    assert depot.nom_valide(" Procédure Veeam (v2).docx ") == "Procédure Veeam (v2).docx"
    # Windows ignore les points et espaces finaux : on les retire plutôt que de créer un nom piège
    assert depot.nom_valide("note. ") == "note"
    # Seul le nom compte, pas le chemin du poste (anciens navigateurs)
    assert depot.nom_depose("C:\\Users\\alice\\Bureau\\note.docx") == "note.docx"
    assert depot.nom_depose("dossier/note.pdf") == "note.pdf"


def test_dossiers():
    assert depot.dossier_valide("") == ""
    assert depot.dossier_valide(" Logiciels / Veeam\\ ") == "Logiciels\\Veeam"
    for dossier in ("..\\autre", "a\\..\\..\\b", "Logiciels\\Ve:eam", "\\\\serveur\\partage\\..\\x"):
        with pytest.raises(depot.DepotRefuse):
            depot.dossier_valide(dossier)


@pytest.mark.parametrize("nom", ["coffre.kdbx", "cle.pem", "outil.exe", "lib.dll", "raccourci.lnk", "sans_extension"])
def test_extensions_refusees(nom):
    with pytest.raises(depot.DepotRefuse):
        depot.extension_acceptee(nom)




def test_configuration_du_depot():
    valeurs = {**parametres.DEFAUTS, "attente_depot": "attente", "sources": [
        {"nom": "GED DSI", "chemin": r"C:\GED", "pole": "DSI", "depot": True},
        {"nom": "Infra", "chemin": r"C:\Infra", "pole": "Infra", "depot": True},
        {"nom": "Métier", "chemin": r"C:\Métier", "pole": "", "depot": False},
    ]}
    cfg = parametres.config(Connexion("postgresql://x"), valeurs)
    assert [s.nom for s in cfg.sources_du_depot()] == ["GED DSI", "Infra"]
    assert cfg.source_du_depot("Infra").nom == "Infra"
    assert cfg.source_du_depot("Métier") is None
    assert cfg.sources[2].pole == "Métier"  # pôle vide : le nom de la source
    assert cfg.attente_depot == Path.cwd() / "attente"
    assert parametres.config(Connexion("postgresql://x"), parametres.DEFAUTS).sources_du_depot() == []


# --- Contrôles d'accès (sans base de données : refusés avant toute requête SQL) -------------


def config_web(tmp_path: Path, **autres) -> Config:
    """Trois sources : la GED peut ranger dans « GED DSI » et « Partage », pas dans « Lecture seule »."""
    return Config(
        url_base="postgresql://ged@127.0.0.1:1/ged",
        sources=[Source(nom, tmp_path / dossier, "DSI")
                 for nom, dossier in (("GED DSI", "ged"), ("Partage", "partage"), ("Lecture seule", "lecture"))],
        sources_depot=["GED DSI", "Partage"], attente_depot=tmp_path / "attente", **autres,
    )


def test_depot_desactive():
    client = TestClient(creer_app(Config(url_base="postgresql://ged@127.0.0.1:1/ged", sources=[], authentification=False)))
    assert client.get("/depot").status_code == 404
    assert client.post("/depot", content=b"x", headers={"Origin": "http://testserver"}).status_code == 404


def test_envoi_depuis_un_autre_site_refuse(tmp_path):
    client = TestClient(creer_app(config_web(tmp_path, authentification=False)), base_url="http://vm-ged:8000")
    for entetes in ({"Origin": "http://site-malveillant.example"}, {}):
        reponse = client.post("/depot", content=b"contenu", headers={"X-Nom-Fichier": "note.txt", **entetes})
        assert reponse.status_code == 403
        reponse = client.post("/depot/1/classer", content="nom=x", headers={
            "Content-Type": "application/x-www-form-urlencoded", **entetes})
        assert reponse.status_code == 403
    assert not (tmp_path / "attente").exists()


def test_fichier_trop_gros_refuse_avant_lecture(tmp_path):
    client = TestClient(creer_app(config_web(tmp_path, authentification=False, taille_max_depot_mo=1)),
                        base_url="http://vm-ged:8000")
    reponse = client.post("/depot", content=b"x" * (1024 * 1024 + 1),
                          headers={"X-Nom-Fichier": "gros.pdf", "Origin": "http://vm-ged:8000"})
    assert reponse.status_code == 413


def test_page_de_validation_reservee_aux_administrateurs(tmp_path):
    from tests.test_authentification import NegociateurFactice, ntlm

    client = TestClient(creer_app(config_web(tmp_path), NegociateurFactice()), base_url="http://vm-ged:8000")
    client.get("/connexion", headers=ntlm(b"reponse"), follow_redirects=False)
    assert client.get("/admin/depots").status_code == 403


# --- Sur une base PostgreSQL de test (GED_TEST_BASE, fixture conn) ----------------------------

TEXTE_VEEAM = "Restauration Veeam : relancer le job de sauvegarde Veeam, vérifier le référentiel de sauvegarde."


@pytest.fixture
def cfg(tmp_path, conn):
    """Sources de config_web, avec quelques documents déjà indexés."""
    from ged import taxonomie_base
    from ged.indexeur import Indexeur
    from ged.taxonomie import lire_toml

    taxonomie_base.importer(conn, lire_toml(TAXONOMIE), "test")
    configuration = config_web(tmp_path, authentification=False)
    fichiers = {
        "partage/Sauvegardes/Veeam/Relance du job.txt": TEXTE_VEEAM,
        "partage/Sauvegardes/Veeam/Référentiel plein.txt": "Référentiel de sauvegarde Veeam plein : purger les points.",
        "partage/Impression/Spouleur.txt": "Imprimante bloquée : relancer le spouleur d'impression.",
        "lecture/Veeam/Copie.txt": TEXTE_VEEAM,
    }
    for chemin, texte in fichiers.items():
        (tmp_path / chemin).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / chemin).write_text(texte, encoding="utf-8")
    (tmp_path / "ged").mkdir()
    (tmp_path / "attente").mkdir()
    Indexeur(configuration, conn).indexer()
    # Le dépôt valide ses transactions : la base de test vidée par la fixture l'est pour de bon
    # (sinon le verrou du TRUNCATE bloquerait les connexions de l'application web)
    conn.commit()
    return configuration


def recevoir(cfg: Config, conn, nom: str, texte: str, lecteur: Lecteur = ALICE, commentaire: str = "") -> int:
    """Simule la réception par la page web : fichier écrit dans la zone d'attente, puis enregistré."""
    recu = cfg.attente_depot / "reception.part"
    recu.write_text(texte, encoding="utf-8")
    return depot.enregistrer(conn, cfg, recu, nom, lecteur, commentaire)


def id_tag(conn, libelle: str) -> int:
    return conn.execute("SELECT id FROM tag WHERE libelle = %s", (libelle,)).fetchone()["id"]


def tags_de(conn, document_id: int) -> dict[str, str]:
    return {
        r["libelle"]: r["origine"]
        for r in conn.execute(
            "SELECT t.libelle, dt.origine FROM document_tag dt JOIN tag t ON t.id = dt.tag_id WHERE dt.document_id = %s",
            (document_id,),
        )
    }


def test_reception_et_propositions(cfg, conn):
    depot_id = recevoir(cfg, conn, "Echec sauvegarde.txt", "Le job Veeam a échoué : relancer le job de sauvegarde Veeam.",
                        commentaire=" urgent ")
    ligne = depot.depot(conn, depot_id)
    assert ligne["statut"] == "attente" and ligne["envoye_le"] is None
    assert ligne["depose_par"] == "DOM\\alice" and ligne["commentaire"] == "urgent"
    assert ligne["tags_proposes"] == [id_tag(conn, "Veeam")]
    # Le dossier des documents semblables d'abord ; jamais une source où la GED n'écrit pas
    premier = ligne["emplacements"][0]
    assert (premier["source"], premier["dossier"]) == ("Partage", "Sauvegardes\\Veeam")
    assert "Relance du job.txt" in premier["exemples"]
    assert all(e["source"] != "Lecture seule" for e in ligne["emplacements"])
    # Le fichier attend sous le nom <id><extension>, la réception n'a rien laissé
    assert [f.name for f in cfg.attente_depot.iterdir()] == [f"{depot_id}.txt"]
    assert [d["id"] for d in depot.a_classer_par(conn, ALICE)] == [depot_id]
    assert depot.a_classer_par(conn, BOB) == []


def test_propositions_sans_mot_commun(cfg, conn):
    ligne = depot.depot(conn, recevoir(cfg, conn, "x.txt", "zzz qqq"))
    assert ligne["emplacements"] == [] and ligne["tags_proposes"] == []


def test_dossiers_des_sources(cfg, conn):
    assert depot.dossiers_des_sources(conn, cfg) == {
        "GED DSI": [], "Partage": ["Impression", "Sauvegardes", "Sauvegardes\\Veeam"],
    }


def test_fichier_refuse_supprime(cfg, conn):
    with pytest.raises(depot.DepotRefuse):
        recevoir(cfg, conn, "outil.exe", "MZ")
    assert list(cfg.attente_depot.iterdir()) == []


def test_administrateur_classe_aussitot(cfg, conn):
    depot_id = recevoir(cfg, conn, "Echec.txt", TEXTE_VEEAM, lecteur=ADMIN)
    glpi = id_tag(conn, "GLPI")
    document_id = depot.classer(conn, cfg, depot_id, ADMIN, "Echec du job", "Partage", "Sauvegardes\\Veeam",
                                [id_tag(conn, "Veeam"), glpi])

    classe = cfg.source_du_depot("Partage").chemin / "Sauvegardes" / "Veeam" / "Echec du job.txt"
    assert classe.read_text(encoding="utf-8") == TEXTE_VEEAM
    assert list(cfg.attente_depot.iterdir()) == []
    doc = conn.execute("SELECT chemin_relatif, statut FROM document WHERE id = %s", (document_id,)).fetchone()
    assert doc == {"chemin_relatif": "Sauvegardes\\Veeam\\Echec du job.txt", "statut": "indexe"}
    # Veeam vient de la taxonomie, GLPI a été ajouté à la main
    assert tags_de(conn, document_id) == {"Veeam": "auto", "GLPI": "manuel"}
    ligne = depot.depot(conn, depot_id)
    assert (ligne["statut"], ligne["traite_par"], ligne["document_id"]) == ("valide", "DOM\\dsi", document_id)

    # Copie identique repérée ; jamais d'écrasement ; jamais dans une source fermée au dépôt
    autre = recevoir(cfg, conn, "Autre.txt", TEXTE_VEEAM, lecteur=ADMIN)
    assert {c["id"] for c in depot.a_classer_par(conn, ADMIN)[0]["copies"]} >= {document_id}
    with pytest.raises(depot.DepotRefuse, match="déjà ce nom"):
        depot.classer(conn, cfg, autre, ADMIN, "Echec du job.txt", "Partage", "Sauvegardes\\Veeam", [])
    with pytest.raises(depot.DepotRefuse, match="ne peut pas ranger"):
        depot.classer(conn, cfg, autre, ADMIN, "Autre.txt", "Lecture seule", "", [])
    assert depot.depot(conn, autre)["statut"] == "attente"
    # Déjà traité : refusé
    with pytest.raises(depot.DepotRefuse, match="déjà été traité"):
        depot.classer(conn, cfg, depot_id, ADMIN, "Encore", "GED DSI", "", [])


def test_tag_propose_decoche_retire(cfg, conn):
    depot_id = recevoir(cfg, conn, "Sauvegarde.txt", TEXTE_VEEAM, lecteur=ADMIN)
    document_id = depot.classer(conn, cfg, depot_id, ADMIN, "Sauvegarde.txt", "GED DSI", "Nouveau", [])
    assert (cfg.source_du_depot("GED DSI").chemin / "Nouveau" / "Sauvegarde.txt").is_file()
    assert tags_de(conn, document_id) == {}
    # Retiré pour de bon : un recalcul ne le remet pas
    assert conn.execute("SELECT count(*) AS nb FROM tag_retire WHERE document_id = %s", (document_id,)).fetchone()["nb"] == 1


def test_utilisateur_envoie_en_validation(cfg, conn):
    depot_id = recevoir(cfg, conn, "Echec.txt", TEXTE_VEEAM)
    with pytest.raises(depot.DepotRefuse, match="pas le vôtre"):
        depot.classer(conn, cfg, depot_id, BOB, "Echec.txt", "Partage", "", [])
    # Emplacement vérifié dès l'envoi
    with pytest.raises(depot.DepotRefuse, match="ne peut pas ranger"):
        depot.classer(conn, cfg, depot_id, ALICE, "Echec.txt", "Lecture seule", "", [])

    assert depot.classer(conn, cfg, depot_id, ALICE, " Echec.txt ", "GED DSI", "Veeam/Incidents", []) is None
    ligne = depot.depot(conn, depot_id)
    assert ligne["choix"] == {"nom": "Echec.txt", "source": "GED DSI", "dossier": "Veeam\\Incidents", "tags": []}
    assert depot.a_classer_par(conn, ALICE) == []
    assert [d["id"] for d in depot.de_la_personne(conn, ALICE)] == [depot_id]
    # Chez l'administrateur : le choix d'Alice en premier
    envoye = depot.envoyes(conn)[0]
    assert envoye["id"] == depot_id and depot.nb_envoyes(conn) == 1
    assert depot.options_emplacement(envoye)[0] == {"source": "GED DSI", "dossier": "Veeam\\Incidents", "exemples": [],
                                                   "choix": True}
    with pytest.raises(depot.DepotRefuse, match="administrateurs"):
        depot.valider(conn, cfg, depot_id, ALICE, "Echec.txt", "GED DSI", "", [])
    depot.valider(conn, cfg, depot_id, ADMIN, "Echec.txt", "GED DSI", "Veeam\\Incidents", [])
    assert (cfg.source_du_depot("GED DSI").chemin / "Veeam" / "Incidents" / "Echec.txt").is_file()


def test_rejet_et_retrait(cfg, conn):
    depot_id = recevoir(cfg, conn, "note.txt", "brouillon")
    with pytest.raises(depot.DepotRefuse, match="Seul"):
        depot.rejeter(conn, cfg, depot_id, BOB)
    depot.rejeter(conn, cfg, depot_id, ALICE, "Retiré par la personne qui l'a déposé")
    ligne = depot.depot(conn, depot_id)
    assert (ligne["statut"], ligne["traite_par"], ligne["contenu"]) == ("rejete", "DOM\\alice", None)
    assert list(cfg.attente_depot.iterdir()) == []
    assert [d["id"] for d in depot.traites(conn)] == [depot_id]


def test_parcours_web(cfg, conn):
    """Dépôt par la page, puis classement sur la même page dans l'emplacement proposé."""
    with TestClient(creer_app(Config(**{**cfg.__dict__, "url_base": os.environ["GED_TEST_BASE"]})),
                    base_url="http://vm-ged:8000") as client:
        origine = {"Origin": "http://vm-ged:8000"}
        reponse = client.post("/depot", content="Job Veeam en échec : relancer le job de sauvegarde.".encode(),
                              headers={**origine, "X-Nom-Fichier": "Veeam%20%C3%A9chec.txt"})
        assert reponse.status_code == 200, reponse.text
        depot_id = reponse.json()["id"]
        page = client.get("/depot").text
        assert "Veeam échec.txt" in page and "Sauvegardes" in page and "Relance du job.txt" in page
        assert client.get(f"/depot/{depot_id}/fichier").content.startswith(b"Job Veeam")

        reponse = client.post(
            f"/depot/{depot_id}/classer", follow_redirects=False,
            content=f"retour=/depot&emplacement=0&nom=Veeam+%C3%A9chec.txt&source=GED+DSI&tag={id_tag(conn, 'Veeam')}",
            headers={**origine, "Content-Type": "application/x-www-form-urlencoded"},
        )
        assert reponse.status_code == 303 and "document=" in reponse.headers["location"]
        assert (cfg.source_du_depot("Partage").chemin / "Sauvegardes" / "Veeam" / "Veeam échec.txt").is_file()

        # « Ailleurs » : la source et le dossier saisis
        depot_id = client.post("/depot", content=b"Note", headers={**origine, "X-Nom-Fichier": "note.txt"}).json()["id"]
        client.post(f"/depot/{depot_id}/classer", content="emplacement=autre&nom=note.txt&source=GED+DSI&dossier=Divers",
                    headers={**origine, "Content-Type": "application/x-www-form-urlencoded"})
        assert (cfg.source_du_depot("GED DSI").chemin / "Divers" / "note.txt").is_file()

        reponse = client.post("/depot", content=b"x", headers={**origine, "X-Nom-Fichier": "coffre.kdbx"})
        assert reponse.status_code == 400 and "KeePass" in reponse.text
