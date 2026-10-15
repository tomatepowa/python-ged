"""Tags posés à la main : réservés aux administrateurs, et respectés par l'indexation."""

from __future__ import annotations

import os
from importlib.resources import files
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ged.config import Config
from ged.web.app import creer_app

# --- Contrôles d'accès (sans base de données : refusés avant toute requête SQL) -------------


@pytest.fixture
def client_sans_identification():
    # Identification désactivée : tout le monde est administrateur (démo), la base est injoignable
    cfg = Config(url_base="postgresql://ged@127.0.0.1:1/ged", sources=[], authentification=False)
    return TestClient(creer_app(cfg), base_url="http://vm-ged:8000")


@pytest.mark.parametrize("url", ["/document/1/tags", "/dossier/tags"])
def test_formulaire_venu_d_un_autre_site_refuse(client_sans_identification, url):
    for entetes in ({"Origin": "http://site-malveillant.example"}, {"Referer": "http://autre:8000/"}, {}):
        reponse = client_sans_identification.post(url, content="ajouter=1&categorie=type&libelle=X", headers={
            "Content-Type": "application/x-www-form-urlencoded", **entetes,
        })
        assert reponse.status_code == 403, entetes
        assert "autre page" in reponse.text


def test_formulaire_refuse_a_qui_n_est_pas_administrateur():
    from tests.test_authentification import NegociateurFactice, ntlm

    cfg = Config(url_base="postgresql://ged@127.0.0.1:1/ged", sources=[])
    client = TestClient(creer_app(cfg, NegociateurFactice()), base_url="http://vm-ged:8000")
    reponse = client.post("/document/1/tags", content="retirer=3", headers={
        "Content-Type": "application/x-www-form-urlencoded", "Origin": "http://vm-ged:8000", **ntlm(b"reponse"),
    })
    assert reponse.status_code == 403
    assert "administrateurs" in reponse.text


# --- Sur une base PostgreSQL de test (GED_TEST_BASE), dans une transaction annulée ----------


@pytest.fixture
def conn():
    url = os.environ.get("GED_TEST_BASE")
    if not url:
        pytest.skip("GED_TEST_BASE non défini (base PostgreSQL de test)")
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(url, row_factory=dict_row) as connexion:
        try:
            connexion.execute(files("ged").joinpath("schema.sql").read_text(encoding="utf-8"))
            connexion.execute("TRUNCATE document_tag, tag_retire, tag_dossier, document, tag, vocabulaire, source "
                              "RESTART IDENTITY CASCADE")
            yield connexion
        finally:
            connexion.rollback()


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


def indexeur(conn, tmp_path: Path, taxonomie: str = '[logiciel]\nVeeam = ["veeam"]\n'):
    from ged.indexeur import Indexeur

    (tmp_path / "taxonomie.toml").write_text(taxonomie, encoding="utf-8")
    return Indexeur(Config(url_base="", sources=[], taxonomie=tmp_path / "taxonomie.toml"), conn)


def test_creation_sans_doublon(conn):
    from ged.tags_manuels import TagInvalide, trouver_ou_creer

    premier = trouver_ou_creer(conn, "Logiciel", "veeam", "DOM\\moi")
    assert trouver_ou_creer(conn, "logiciel", "  VÉEAM ", "DOM\\autre") == premier
    assert conn.execute("SELECT libelle, cree_par FROM tag WHERE id = %s", (premier,)).fetchone() == {
        "libelle": "Veeam", "cree_par": "DOM\\moi",
    }
    with pytest.raises(TagInvalide):
        trouver_ou_creer(conn, "type", " ", "DOM\\moi")


def test_tag_retire_a_la_main_ne_revient_pas(conn, tmp_path):
    from ged import tags_manuels

    doc = creer_documents(conn, "Sauvegardes\\veeam.txt")["Sauvegardes\\veeam.txt"]
    idx = indexeur(conn, tmp_path)
    idx._appliquer_tags(doc, "veeam.txt", "")
    assert tags_de(conn, doc) == {"Veeam": "auto"}

    veeam = conn.execute("SELECT id FROM tag WHERE libelle = 'Veeam'").fetchone()["id"]
    tags_manuels.retirer(conn, doc, veeam, "DOM\\moi")
    idx._appliquer_tags(doc, "veeam.txt", "")   # recalcul : la taxonomie le proposerait encore
    assert tags_de(conn, doc) == {}

    # Reposé à la main : il revient, et ne dépend plus de la taxonomie
    tags_manuels.ajouter(conn, doc, veeam, "DOM\\moi")
    indexeur(conn, tmp_path, taxonomie="")._appliquer_tags(doc, "veeam.txt", "")
    assert tags_de(conn, doc) == {"Veeam": "manuel"}


def test_tag_de_dossier(conn, tmp_path):
    from ged import tags_manuels

    docs = creer_documents(conn, "Applis\\DPI\\a.txt", "Applis\\DPI\\Recette\\b.txt", "Applis\\DPI_old\\c.txt", "d.txt")
    a, b, c, d = docs.values()
    dpi = tags_manuels.trouver_ou_creer(conn, "logiciel", "DPI", "DOM\\moi")
    source = conn.execute("SELECT id FROM source").fetchone()["id"]

    # Sous-dossiers compris, mais pas un dossier voisin au nom qui commence pareil
    assert tags_manuels.ajouter_au_dossier(conn, source, "applis\\dpi", dpi, "DOM\\moi") == 2
    assert [tags_de(conn, x) for x in (a, b, c, d)] == [{"DPI": "dossier"}, {"DPI": "dossier"}, {}, {}]

    # Un recalcul (ou un nouveau document arrivé dans le dossier) le garde
    indexeur(conn, tmp_path)._appliquer_tags(b, "b.txt", "")
    assert tags_de(conn, b) == {"DPI": "dossier"}

    # Hérité dans le sous-dossier, propre au dossier
    regles = {r["chemin"]: r for r in tags_manuels.regles_du_dossier(conn, source, "Applis\\DPI\\Recette")}
    assert regles["applis\\dpi"]["herite"] and regles["applis\\dpi"]["nb"] == 2
    assert not tags_manuels.regles_du_dossier(conn, source, "Applis\\DPI_old")

    # Retiré du dossier : les documents le perdent, sauf celui qui l'a reçu à la main
    tags_manuels.ajouter(conn, a, dpi, "DOM\\moi")
    regle = regles["applis\\dpi"]["id"]
    assert tags_manuels.retirer_du_dossier(conn, regle) == 1
    assert [tags_de(conn, x) for x in (a, b)] == [{"DPI": "manuel"}, {}]


def test_tag_de_dossier_garde_par_un_dossier_parent(conn):
    from ged import tags_manuels

    docs = creer_documents(conn, "Applis\\DPI\\a.txt")
    dpi = tags_manuels.trouver_ou_creer(conn, "logiciel", "DPI", "DOM\\moi")
    source = conn.execute("SELECT id FROM source").fetchone()["id"]
    tags_manuels.ajouter_au_dossier(conn, source, "Applis", dpi, "DOM\\moi")
    tags_manuels.ajouter_au_dossier(conn, source, "Applis\\DPI", dpi, "DOM\\moi")
    regle = next(r for r in tags_manuels.regles_du_dossier(conn, source, "Applis\\DPI") if not r["herite"])
    # Le document reste dans Applis, qui porte aussi le tag
    assert tags_manuels.retirer_du_dossier(conn, regle["id"]) == 0
    assert tags_de(conn, docs["Applis\\DPI\\a.txt"]) == {"DPI": "dossier"}
