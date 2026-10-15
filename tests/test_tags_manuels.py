"""Tags posés à la main et gestion des tags : droits de chacun, respect par l'indexation."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ged.acces import Lecteur
from ged.config import Config
from ged.tags_manuels import peut_retirer
from ged.taxonomie import lire_toml
from ged.web.app import creer_app

ADMIN = Lecteur("DOM\\dsi", frozenset(), frozenset(), admin=True, sid="S-1-5-21-1-2-3-500")
ALICE = Lecteur("DOM\\alice", frozenset(), frozenset(), sid="S-1-5-21-1-2-3-1101")
BOB = Lecteur("DOM\\bob", frozenset(), frozenset(), sid="S-1-5-21-1-2-3-1102")
FORMULAIRE = {"Content-Type": "application/x-www-form-urlencoded"}


def test_qui_peut_retirer_quoi():
    pose_alice = {"origine": "manuel", "pose_par_sid": ALICE.sid}
    assert peut_retirer(ALICE, pose_alice)
    assert not peut_retirer(BOB, pose_alice)
    assert peut_retirer(ADMIN, pose_alice)
    for origine in ("auto", "dossier"):
        assert not peut_retirer(ALICE, {"origine": origine, "pose_par_sid": None})
        assert peut_retirer(ADMIN, {"origine": origine, "pose_par_sid": None})
    # Sans SID connu (identification désactivée sans être admin : impossible en pratique), rien
    assert not peut_retirer(Lecteur("?", frozenset(), frozenset()), {"origine": "manuel", "pose_par_sid": None})


# --- Contrôles d'accès (sans base de données : refusés avant toute requête SQL) -------------


@pytest.fixture
def client_sans_identification():
    # Identification désactivée : tout le monde est administrateur (démo), la base est injoignable
    cfg = Config(url_base="postgresql://ged@127.0.0.1:1/ged", sources=[], authentification=False)
    return TestClient(creer_app(cfg), base_url="http://vm-ged:8000")


@pytest.mark.parametrize("url", ["/document/1/tags", "/dossier/tags", "/admin/tags", "/journal/1/annuler"])
def test_formulaire_venu_d_un_autre_site_refuse(client_sans_identification, url):
    for entetes in ({"Origin": "http://site-malveillant.example"}, {"Referer": "http://autre:8000/"}, {}):
        reponse = client_sans_identification.post(url, content="tag_id=1", headers={**FORMULAIRE, **entetes})
        assert reponse.status_code == 403, entetes
        assert "autre page" in reponse.text


@pytest.fixture
def client_utilisateur():
    from tests.test_authentification import NegociateurFactice, ntlm

    cfg = Config(url_base="postgresql://ged@127.0.0.1:1/ged", sources=[])
    client = TestClient(creer_app(cfg, NegociateurFactice()), base_url="http://vm-ged:8000")
    # Session d'une personne non administratrice (sans suivre la redirection vers l'accueil)
    client.get("/connexion", headers=ntlm(b"reponse"), follow_redirects=False)
    return client


@pytest.mark.parametrize("url", ["/dossier/tags", "/admin/tags"])
def test_tags_de_dossier_et_gestion_reserves_aux_administrateurs(client_utilisateur, url):
    reponse = client_utilisateur.post(url, content="tag_id=1", headers={**FORMULAIRE, "Origin": "http://vm-ged:8000"})
    assert reponse.status_code == 403
    assert "administrateurs" in reponse.text


@pytest.mark.parametrize("url", ["/admin/tags", "/admin/journal"])
def test_pages_d_administration_reservees(client_utilisateur, url):
    assert client_utilisateur.get(url).status_code == 403


# --- Sur une base PostgreSQL de test (GED_TEST_BASE, fixture conn), transaction annulée -----


def creer_documents(conn, *chemins: str) -> dict[str, int]:
    source = conn.execute("INSERT INTO source (nom, chemin, pole) VALUES ('Infra', 'x', 'Infra') RETURNING id").fetchone()["id"]
    ids = {}
    for chemin in chemins:
        ids[chemin] = conn.execute(
            "INSERT INTO document (source_id, chemin, chemin_relatif, nom, extension, famille, taille, modifie_le, statut) "
            "VALUES (%s, %s, %s, %s, '.txt', 'Texte', 1, now(), 'indexe') RETURNING id",
            (source, "x\\" + chemin, chemin, Path(chemin).name),
        ).fetchone()["id"]
    return ids


def tags_de(conn, document_id: int) -> dict[str, str]:
    return {
        ligne["libelle"]: ligne["origine"]
        for ligne in conn.execute(
            "SELECT t.libelle, dt.origine FROM document_tag dt JOIN tag t ON t.id = dt.tag_id WHERE dt.document_id = %s",
            (document_id,),
        )
    }


def indexeur(conn, tmp_path: Path, texte: str = '[logiciel]\nVeeam = ["veeam"]\n'):
    """Indexeur avec cette taxonomie (au format de taxonomie.toml) importée en base."""
    from ged import taxonomie_base
    from ged.indexeur import Indexeur

    taxonomie_base.importer(conn, lire_toml(texte), "test")
    return Indexeur(Config(url_base="", sources=[], taxonomie=tmp_path / "taxonomie.toml"), conn)


def test_creation_sans_doublon_et_renommage(conn):
    from ged import tags_manuels

    veeam = tags_manuels.creer(conn, "Logiciel", "veeam", ADMIN)
    assert conn.execute("SELECT categorie, libelle, cree_par FROM tag WHERE id = %s", (veeam,)).fetchone() == {
        "categorie": "logiciel", "libelle": "Veeam", "cree_par": "DOM\\dsi",
    }
    with pytest.raises(tags_manuels.TagInvalide, match="existe déjà"):
        tags_manuels.creer(conn, "logiciel", "  VÉEAM ", ADMIN)
    dpi = tags_manuels.creer(conn, "logiciel", "DPI", ADMIN)
    with pytest.raises(tags_manuels.TagInvalide, match="fusionnez"):
        tags_manuels.renommer(conn, dpi, "logiciel", "veeam", ADMIN)
    tags_manuels.renommer(conn, dpi, "logiciel", "Dossier patient", ADMIN)
    assert [j["action"] for j in tags_manuels.journal(conn)] == ["renommage", "création", "création"]


def test_utilisateur_retire_seulement_sa_pose(conn, tmp_path):
    from ged import tags_manuels

    doc = creer_documents(conn, "Sauvegardes\\veeam.txt")["Sauvegardes\\veeam.txt"]
    idx = indexeur(conn, tmp_path)
    idx._appliquer_tags(doc, "veeam.txt", "")
    veeam = conn.execute("SELECT id FROM tag WHERE libelle = 'Veeam'").fetchone()["id"]
    procedure = tags_manuels.creer(conn, "type", "Procédure", ADMIN)

    tags_manuels.ajouter(conn, doc, procedure, ALICE)
    tags_manuels.ajouter(conn, doc, veeam, ALICE)   # aussi proposé par la taxonomie
    tags_manuels.ajouter(conn, doc, procedure, BOB)  # déjà posé : reste au nom d'Alice
    assert tags_de(conn, doc) == {"Veeam": "manuel", "Procédure": "manuel"}

    with pytest.raises(tags_manuels.RetraitRefuse):
        tags_manuels.retirer(conn, doc, procedure, BOB, idx.taxonomie)
    tags_manuels.retirer(conn, doc, procedure, ALICE, idx.taxonomie)
    # Alice annule sa pose de Veeam : la taxonomie le lui redonne aussitôt
    tags_manuels.retirer(conn, doc, veeam, ALICE, idx.taxonomie)
    assert tags_de(conn, doc) == {"Veeam": "auto"}
    with pytest.raises(tags_manuels.RetraitRefuse):
        tags_manuels.retirer(conn, doc, veeam, ALICE, idx.taxonomie)


def test_retrait_par_un_administrateur_definitif(conn, tmp_path):
    from ged import tags_manuels

    doc = creer_documents(conn, "Sauvegardes\\veeam.txt")["Sauvegardes\\veeam.txt"]
    idx = indexeur(conn, tmp_path)
    idx._appliquer_tags(doc, "veeam.txt", "")
    veeam = conn.execute("SELECT id FROM tag WHERE libelle = 'Veeam'").fetchone()["id"]
    tags_manuels.retirer(conn, doc, veeam, ADMIN, idx.taxonomie)
    idx._appliquer_tags(doc, "veeam.txt", "")   # recalcul : la taxonomie le proposerait encore
    assert tags_de(conn, doc) == {}
    # Reposé à la main : il revient, et ne dépend plus de la taxonomie
    tags_manuels.ajouter(conn, doc, veeam, ALICE)
    indexeur(conn, tmp_path, texte="")._appliquer_tags(doc, "veeam.txt", "")
    assert tags_de(conn, doc) == {"Veeam": "manuel"}
    assert [j["action"] for j in tags_manuels.journal(conn, par="DOM\\dsi")] == ["retrait"]


def test_tag_de_dossier(conn, tmp_path):
    from ged import tags_manuels

    docs = creer_documents(conn, "Applis\\DPI\\a.txt", "Applis\\DPI\\Recette\\b.txt", "Applis\\DPI_old\\c.txt", "d.txt")
    a, b, c, d = docs.values()
    dpi = tags_manuels.creer(conn, "logiciel", "DPI", ADMIN)
    source = conn.execute("SELECT id FROM source").fetchone()["id"]

    # Sous-dossiers compris, mais pas un dossier voisin au nom qui commence pareil
    assert tags_manuels.ajouter_au_dossier(conn, source, "applis\\dpi", dpi, ADMIN) == 2
    assert [tags_de(conn, x) for x in (a, b, c, d)] == [{"DPI": "dossier"}, {"DPI": "dossier"}, {}, {}]

    # Un recalcul (ou un nouveau document arrivé dans le dossier) le garde
    indexeur(conn, tmp_path)._appliquer_tags(b, "b.txt", "")
    assert tags_de(conn, b) == {"DPI": "dossier"}

    # Hérité dans le sous-dossier, propre au dossier
    regles = {r["chemin"]: r for r in tags_manuels.regles_du_dossier(conn, source, "Applis\\DPI\\Recette")}
    assert regles["applis\\dpi"]["herite"] and regles["applis\\dpi"]["nb"] == 2
    assert not tags_manuels.regles_du_dossier(conn, source, "Applis\\DPI_old")

    # Retiré du dossier : les documents le perdent, sauf celui qui l'a reçu à la main
    tags_manuels.ajouter(conn, a, dpi, ALICE)
    assert tags_manuels.retirer_du_dossier(conn, regles["applis\\dpi"]["id"], ADMIN) == 1
    assert [tags_de(conn, x) for x in (a, b)] == [{"DPI": "manuel"}, {}]


def test_tag_de_dossier_garde_par_un_dossier_parent(conn):
    from ged import tags_manuels

    docs = creer_documents(conn, "Applis\\DPI\\a.txt")
    dpi = tags_manuels.creer(conn, "logiciel", "DPI", ADMIN)
    source = conn.execute("SELECT id FROM source").fetchone()["id"]
    tags_manuels.ajouter_au_dossier(conn, source, "Applis", dpi, ADMIN)
    tags_manuels.ajouter_au_dossier(conn, source, "Applis\\DPI", dpi, ADMIN)
    regle = next(r for r in tags_manuels.regles_du_dossier(conn, source, "Applis\\DPI") if not r["herite"])
    # Le document reste dans Applis, qui porte aussi le tag
    assert tags_manuels.retirer_du_dossier(conn, regle["id"], ADMIN) == 0
    assert tags_de(conn, docs["Applis\\DPI\\a.txt"]) == {"DPI": "dossier"}


def test_fusion_et_suppression(conn, tmp_path):
    from ged import tags_manuels

    a, b = creer_documents(conn, "Sauvegardes\\veeam.txt", "Sauvegardes\\b.txt").values()
    idx = indexeur(conn, tmp_path)
    idx._appliquer_tags(a, "veeam.txt", "")
    veeam = conn.execute("SELECT id FROM tag WHERE libelle = 'Veeam'").fetchone()["id"]
    doublon = tags_manuels.creer(conn, "logiciel", "Veeam Backup", ADMIN)
    source = conn.execute("SELECT id FROM source").fetchone()["id"]
    tags_manuels.ajouter(conn, a, doublon, ALICE)
    tags_manuels.ajouter_au_dossier(conn, source, "Sauvegardes", doublon, ADMIN)

    # La pose à la main d'Alice l'emporte sur la proposition de la taxonomie ; le dossier suit
    assert tags_manuels.fusionner(conn, doublon, veeam, ADMIN) == 2
    assert tags_de(conn, a) == {"Veeam": "manuel"} and tags_de(conn, b) == {"Veeam": "dossier"}
    assert conn.execute("SELECT count(*) AS nb FROM tag WHERE id = %s", (doublon,)).fetchone()["nb"] == 0
    assert tags_manuels.regles_du_dossier(conn, source, "Sauvegardes")[0]["tag_id"] == veeam
    tags_manuels.retirer(conn, a, veeam, ALICE, idx.taxonomie)  # sa pose, transférée, reste à son nom
    assert tags_de(conn, a) == {"Veeam": "auto"}

    assert tags_manuels.supprimer(conn, veeam, ADMIN) == 2
    assert tags_de(conn, a) == tags_de(conn, b) == {}
    # Le journal garde le nom du tag disparu
    assert tags_manuels.journal(conn)[0]["tag"] == "Veeam (logiciel)"


def test_annuler_le_retrait_d_un_administrateur(conn, tmp_path):
    from ged import tags_manuels

    doc = creer_documents(conn, "veeam.txt")["veeam.txt"]
    idx = indexeur(conn, tmp_path)
    idx._appliquer_tags(doc, "veeam.txt", "")
    veeam = tags_manuels.id_tag(conn, "logiciel", "Veeam")
    retrait = tags_manuels.retirer(conn, doc, veeam, ADMIN, idx.taxonomie)
    assert tags_de(conn, doc) == {}
    ligne = tags_manuels.ligne_du_journal(conn, retrait)
    assert tags_manuels.annulable(ligne, ADMIN) and not tags_manuels.annulable(ligne, ALICE)
    with pytest.raises(tags_manuels.RetraitRefuse):
        tags_manuels.annuler(conn, retrait, ALICE)
    assert "remis" in tags_manuels.annuler(conn, retrait, ADMIN)
    # De retour comme avant : posé par la taxonomie, plus noté comme retiré
    assert tags_de(conn, doc) == {"Veeam": "auto"}
    assert not conn.execute("SELECT 1 FROM tag_retire").fetchone()
    idx._appliquer_tags(doc, "veeam.txt", "")
    assert tags_de(conn, doc) == {"Veeam": "auto"}
    with pytest.raises(tags_manuels.TagInvalide, match="Déjà annulé"):
        tags_manuels.annuler(conn, retrait, ADMIN)
    assert [j["action"] for j in tags_manuels.journal(conn)][:2] == ["annulation", "retrait"]
    # Tag supprimé entre-temps : rien à remettre
    retrait = tags_manuels.retirer(conn, doc, veeam, ADMIN, idx.taxonomie)
    tags_manuels.supprimer(conn, veeam, ADMIN)
    with pytest.raises(tags_manuels.TagInvalide, match="n'existe plus"):
        tags_manuels.annuler(conn, retrait, ADMIN)


def test_annuler_le_retrait_de_sa_pose(conn, tmp_path):
    from ged import tags_manuels

    doc = creer_documents(conn, "note.txt")["note.txt"]
    idx = indexeur(conn, tmp_path)
    dpi = tags_manuels.creer(conn, "logiciel", "DPI", ADMIN)
    tags_manuels.ajouter(conn, doc, dpi, ALICE)
    pose_le = conn.execute("SELECT pose_le FROM document_tag").fetchone()["pose_le"]
    retrait = tags_manuels.retirer(conn, doc, dpi, ALICE, idx.taxonomie)
    with pytest.raises(tags_manuels.RetraitRefuse):
        tags_manuels.annuler(conn, retrait, BOB)
    tags_manuels.annuler(conn, retrait, ALICE)
    # Remis à son nom, avec sa date : elle peut toujours le retirer
    assert conn.execute("SELECT origine, pose_par, pose_par_sid, pose_le FROM document_tag").fetchone() == {
        "origine": "manuel", "pose_par": ALICE.compte, "pose_par_sid": ALICE.sid, "pose_le": pose_le,
    }


def test_annuler_le_retrait_d_un_dossier(conn):
    from ged import tags_manuels

    docs = creer_documents(conn, r"applis\a.txt", r"applis\b.txt")
    source = conn.execute("SELECT id FROM source").fetchone()["id"]
    dpi = tags_manuels.creer(conn, "logiciel", "DPI", ADMIN)
    tags_manuels.ajouter_au_dossier(conn, source, "applis", dpi, ADMIN)
    regle = tags_manuels.regles_du_dossier(conn, source, "applis")[0]
    assert tags_manuels.retirer_du_dossier(conn, regle["id"], ADMIN) == 2
    retrait = tags_manuels.journal(conn)[0]["id"]
    assert "2 documents" in tags_manuels.annuler(conn, retrait, ADMIN)
    assert [r["chemin"] for r in tags_manuels.regles_du_dossier(conn, source, "applis")] == ["applis"]
    assert all(tags_de(conn, d) == {"DPI": "dossier"} for d in docs.values())
