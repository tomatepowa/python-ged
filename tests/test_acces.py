"""Filtrage des documents selon les droits Windows de la personne connectée."""

from __future__ import annotations

import os
import sys
from importlib.resources import files

import pytest

from ged.acces import TOUT, Lecteur, diagnostic_acces, filtre_acces, peut_lire, sids_accordant
from ged.droits import ECRITURE, GENERIC_READ, LECTURE, MODIFICATION, acces_lecture

INFRA = "S-1-5-21-1-2-3-1101"
STAGIAIRES = "S-1-5-21-1-2-3-1102"
UTILISATEURS = "S-1-5-21-1-2-3-513"
ADMINS_LOCAUX = "S-1-5-32-544"


def lecteur(*sids: str, refus_seulement: tuple[str, ...] = ()) -> Lecteur:
    return Lecteur("CLINIQUE\\x", frozenset(sids), frozenset(sids) | frozenset(refus_seulement))


def test_acces_lecture_sans_refus():
    sids, ordre = acces_lecture([(INFRA, MODIFICATION, False), (UTILISATEURS, ECRITURE, False), (INFRA, LECTURE, False)])
    # L'écriture seule ne permet pas de lire ; un SID n'est gardé qu'une fois ; pas de refus, pas d'ordre
    assert sids == [INFRA]
    assert ordre is None


def test_acces_lecture_avec_refus_garde_l_ordre():
    sids, ordre = acces_lecture([(STAGIAIRES, LECTURE, True), (UTILISATEURS, GENERIC_READ, False)])
    assert sids == [UTILISATEURS]
    assert ordre == [[STAGIAIRES, "R"], [UTILISATEURS, "A"]]


@pytest.mark.parametrize(
    "acl, personne, attendu",
    [
        # Refus explicite avant l'autorisation héritée : refusé
        ([[STAGIAIRES, "R"], [UTILISATEURS, "A"]], lecteur(STAGIAIRES, UTILISATEURS), False),
        ([[STAGIAIRES, "R"], [UTILISATEURS, "A"]], lecteur(UTILISATEURS), True),
        # Autorisation explicite avant un refus hérité : Windows autorise
        ([[INFRA, "A"], [STAGIAIRES, "R"]], lecteur(INFRA, STAGIAIRES), True),
        # Groupe « refus seulement » (ex. Administrateurs filtré par l'UAC) : compte pour les refus...
        ([[ADMINS_LOCAUX, "R"], [UTILISATEURS, "A"]], lecteur(UTILISATEURS, refus_seulement=(ADMINS_LOCAUX,)), False),
        # ... mais jamais pour les autorisations
        ([[ADMINS_LOCAUX, "A"]], lecteur(UTILISATEURS, refus_seulement=(ADMINS_LOCAUX,)), False),
        # Aucune entrée ne concerne la personne : refusé
        ([[INFRA, "A"]], lecteur(UTILISATEURS), False),
    ],
)
def test_peut_lire(acl, personne, attendu):
    assert peut_lire(acl, personne.sids, personne.sids_refus) is attendu


def test_administrateur_sans_filtre():
    admin = Lecteur("CLINIQUE\\dsi", frozenset({INFRA}), frozenset({INFRA}), admin=True)
    assert filtre_acces(None, admin) is TOUT
    assert filtre_acces(None, Lecteur.sans_filtre()) is TOUT


@pytest.mark.skipif(sys.platform != "win32", reason="droits NTFS : Windows uniquement")
def test_entrees_d_une_vraie_dacl():
    import win32security

    from ged.droits import INHERIT_ONLY_ACE, entrees_dacl

    def sid(texte):
        return win32security.ConvertStringSidToSid(texte)

    dacl = win32security.ACL()
    dacl.AddAccessDeniedAce(win32security.ACL_REVISION, LECTURE, sid(STAGIAIRES))
    dacl.AddAccessAllowedAce(win32security.ACL_REVISION, MODIFICATION, sid(INFRA))
    # Ne s'applique qu'aux fichiers créés dans le dossier : ignorée
    dacl.AddAccessAllowedAceEx(win32security.ACL_REVISION, INHERIT_ONLY_ACE, LECTURE, sid(UTILISATEURS))
    entrees = entrees_dacl(dacl)
    assert entrees == [(STAGIAIRES, LECTURE, True, False), (INFRA, MODIFICATION, False, False)]
    assert acces_lecture([e[:3] for e in entrees]) == ([INFRA], [[STAGIAIRES, "R"], [INFRA, "A"]])


# --- Filtre SQL ------------------------------------------------------------------------------
# Sur une base PostgreSQL de test (GED_TEST_BASE="postgresql://..."), dans une transaction
# annulée à la fin : rien n'est écrit. Ignoré sans cette variable.

@pytest.fixture
def base_de_test():
    url = os.environ.get("GED_TEST_BASE")
    if not url:
        pytest.skip("GED_TEST_BASE non défini (base PostgreSQL de test)")
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(url, row_factory=dict_row) as conn:
        try:
            conn.execute(files("ged").joinpath("schema.sql").read_text(encoding="utf-8"))
            yield conn
        finally:
            conn.rollback()


def test_ged_peut_lire_comme_python(base_de_test):
    cas = [
        ([[STAGIAIRES, "R"], [UTILISATEURS, "A"]], lecteur(STAGIAIRES, UTILISATEURS)),
        ([[STAGIAIRES, "R"], [UTILISATEURS, "A"]], lecteur(UTILISATEURS)),
        ([[INFRA, "A"], [STAGIAIRES, "R"]], lecteur(INFRA, STAGIAIRES)),
        ([[ADMINS_LOCAUX, "R"], [UTILISATEURS, "A"]], lecteur(UTILISATEURS, refus_seulement=(ADMINS_LOCAUX,))),
        ([[INFRA, "A"]], lecteur(UTILISATEURS)),
    ]
    from psycopg.types.json import Jsonb

    for acl, personne in cas:
        sql = base_de_test.execute(
            "SELECT ged_peut_lire(%s, %s::text[], %s::text[]) AS ok",
            (Jsonb(acl), sorted(personne.sids), sorted(personne.sids_refus)),
        ).fetchone()["ok"]
        assert sql is peut_lire(acl, personne.sids, personne.sids_refus), acl


def test_filtre_sql_des_documents(base_de_test):
    from psycopg.types.json import Jsonb

    conn = base_de_test
    conn.execute("TRUNCATE document_tag, document, tag, vocabulaire, source RESTART IDENTITY CASCADE")
    infra = conn.execute(
        "INSERT INTO source (nom, chemin, pole) VALUES ('Infra', 'x', 'Infra') RETURNING id"
    ).fetchone()["id"]
    # Partage dont les stagiaires sont exclus
    verrouillee = conn.execute(
        "INSERT INTO source (nom, chemin, pole, acces_partage) VALUES ('RH', 'y', 'RH', %s) RETURNING id",
        (Jsonb([[STAGIAIRES, "R"], [UTILISATEURS, "A"]]),),
    ).fetchone()["id"]
    documents = {
        "tous": (infra, [UTILISATEURS], None),
        "infra": (infra, [INFRA], None),
        "sauf_stagiaires": (infra, [UTILISATEURS], [[STAGIAIRES, "R"], [UTILISATEURS, "A"]]),
        "illisible": (infra, None, None),
        "partage_rh": (verrouillee, [UTILISATEURS], None),
    }
    for nom, (source, sids, ordre) in documents.items():
        conn.execute(
            "INSERT INTO document (source_id, chemin, chemin_relatif, nom, extension, famille, taille, "
            "modifie_le, statut, acces_sids, acces_ordre) "
            "VALUES (%s, %s, %s, %s, '.txt', 'Texte', 1, now(), 'indexe', %s, %s)",
            (source, nom, nom, nom, sids, Jsonb(ordre) if ordre else None),
        )

    def visibles(personne: Lecteur) -> set[str]:
        filtre = filtre_acces(conn, personne)
        return {
            ligne["nom"]
            for ligne in conn.execute(f"SELECT d.nom FROM document d WHERE {filtre.sql}", filtre.parametres)
        }

    assert visibles(lecteur(UTILISATEURS)) == {"tous", "sauf_stagiaires", "partage_rh"}
    assert visibles(lecteur(UTILISATEURS, INFRA)) == {"tous", "infra", "sauf_stagiaires", "partage_rh"}
    assert visibles(lecteur(UTILISATEURS, STAGIAIRES)) == {"tous"}
    assert visibles(lecteur(INFRA)) == {"infra"}
    admin = Lecteur("CLINIQUE\\dsi", frozenset(), frozenset(), admin=True)
    assert visibles(admin) == set(documents)


def test_diagnostic_quand_aucun_document_n_est_visible(base_de_test):
    conn = base_de_test
    conn.execute("TRUNCATE document_tag, document, tag, vocabulaire, source RESTART IDENTITY CASCADE")
    assert diagnostic_acces(conn) == "index_vide"

    source = conn.execute("INSERT INTO source (nom, chemin, pole) VALUES ('Infra', 'x', 'Infra') RETURNING id").fetchone()["id"]

    def ajouter(nom, sids):
        conn.execute(
            "INSERT INTO document (source_id, chemin, chemin_relatif, nom, extension, famille, taille, "
            "modifie_le, statut, acces_sids) VALUES (%s, %s, %s, %s, '.txt', 'Texte', 1, now(), 'indexe', %s)",
            (source, nom, nom, nom, sids),
        )

    # Indexé avant la mise en place du filtrage : droits pas encore enregistrés
    ajouter("ancien", None)
    assert diagnostic_acces(conn) == "droits_absents"
    ajouter("recent", [INFRA])
    assert diagnostic_acces(conn) == "aucun_droit"


@pytest.mark.parametrize(
    "sids, ordre, personne, attendu",
    [
        # Sans refus : tous ses groupes autorisés
        ([INFRA, UTILISATEURS], None, lecteur(UTILISATEURS, INFRA), [INFRA, UTILISATEURS]),
        ([INFRA], None, lecteur(UTILISATEURS), []),
        # Avec refus : seules les autorisations placées avant le premier refus qui la vise
        ([INFRA, UTILISATEURS], [[INFRA, "A"], [STAGIAIRES, "R"], [UTILISATEURS, "A"]],
         lecteur(INFRA, STAGIAIRES, UTILISATEURS), [INFRA]),
        ([UTILISATEURS], [[STAGIAIRES, "R"], [UTILISATEURS, "A"]], lecteur(UTILISATEURS), [UTILISATEURS]),
        ([UTILISATEURS], None, lecteur(), []),
    ],
)
def test_sids_accordant(sids, ordre, personne, attendu):
    assert sids_accordant(sids, ordre, personne) == attendu


def test_variantes_nom():
    from ged.recherche import variantes_nom

    assert variantes_nom("Antoine Cosson", "acosson", "") == ["acosson", "antoine cosson", "cosson antoine"]
    # Accents, majuscules et ponctuation ignorés
    assert "helene dupre" in variantes_nom("DUPRÉ, Hélène")


def test_mes_documents_et_filtre_personne(base_de_test):
    from ged.recherche import Filtres, mes_documents, rechercher

    conn = base_de_test
    conn.execute("TRUNCATE document_tag, document, tag, vocabulaire, source RESTART IDENTITY CASCADE")
    source = conn.execute("INSERT INTO source (nom, chemin, pole) VALUES ('Infra', 'x', 'Infra') RETURNING id").fetchone()["id"]
    moi = "CLINIQUE" + chr(92) + "acosson"
    for nom, auteur, modificateur, proprietaire, sids in [
        ("a", "Antoine Cosson", "Hélène Dupré", "CLINIQUE" + chr(92) + "hdupre", [UTILISATEURS]),
        ("b", "COSSON Antoine", "Cosson, Antoine", moi, [UTILISATEURS]),
        ("c", "Hélène Dupré", "acosson", moi.upper(), [UTILISATEURS]),
        # Écrit par moi, mais je ne peux plus le lire : pas compté
        ("d", "Antoine Cosson", None, moi, [INFRA]),
    ]:
        conn.execute(
            "INSERT INTO document (source_id, chemin, chemin_relatif, nom, extension, famille, taille, modifie_le, "
            "statut, auteur, dernier_modificateur, proprietaire, acces_sids) "
            "VALUES (%s, %s, %s, %s, '.docx', 'Word', 1, now(), 'indexe', %s, %s, %s, %s)",
            (source, nom, nom, nom, auteur, modificateur, proprietaire, sids),
        )
    filtre = filtre_acces(conn, lecteur(UTILISATEURS))
    personnes = ["Antoine Cosson", "acosson", moi]
    assert mes_documents(conn, filtre, personnes) == {"auteur": 2, "modificateur": 2, "proprietaire": 2}

    # Le filtre « personne » de la recherche retrouve exactement les documents comptés
    def trouves(role, noms=personnes):
        r = rechercher(conn, Filtres(personnes=noms, role=role, vue="liste"), filtre)
        return sorted(d["nom"] for d in r.documents)

    assert trouves("auteur") == ["a", "b"]
    assert trouves("modificateur") == ["b", "c"]
    assert trouves("proprietaire") == ["b", "c"]
    assert trouves("") == ["a", "b", "c"]
    # Depuis une fiche : un seul nom, tel qu'écrit dans le document
    assert trouves("auteur", ["Hélène Dupré"]) == ["c"]


def test_lien_du_filtre_personne():
    from ged.recherche import Filtres

    lien = Filtres(personnes=["Antoine Cosson", "acosson"], role="auteur", vue="liste").lien()
    assert lien == "vue=liste&role=auteur&personne=Antoine+Cosson&personne=acosson"
    # Sans personne, pas de rôle orphelin dans l'adresse
    assert Filtres(role="auteur").lien() == ""
