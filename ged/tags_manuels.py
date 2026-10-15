"""Tags posés à la main depuis l'interface (réservé aux administrateurs de la GED).

Trois façons de compléter ou corriger la taxonomie :
- ajouter un tag à un document (origine « manuel ») : il survit aux recalculs ;
- retirer un tag d'un document : noté dans tag_retire, la taxonomie ne le remet plus ;
- poser un tag sur un dossier (table tag_dossier) : tous ses documents le portent (origine
  « dossier »), sous-dossiers compris, y compris ceux qui y arriveront plus tard.

Un tag inexistant peut être créé à la volée : il est enregistré en base (tag.cree_par), pas dans
taxonomie.toml, et ne s'applique donc qu'à la main.
"""

from __future__ import annotations

import psycopg

LIBELLE_MAX = 60

def dans_le_dossier(r: str = "r", d: str = "d") -> str:
    """Condition SQL « le document `d` est dans le dossier de la règle `r` » (source_id, chemin),
    sous-dossiers compris. Sans casse, comme Windows ; pas de LIKE, que « _ » et « % » fausseraient."""
    return (
        f"{d}.source_id = {r}.source_id AND ({r}.chemin = '' "
        f"OR left(lower({d}.chemin_relatif), length({r}.chemin) + 1) = lower({r}.chemin) || '\\')"
    )


class TagInvalide(ValueError):
    pass


def categories(conn: psycopg.Connection) -> list[str]:
    return [ligne["categorie"] for ligne in conn.execute("SELECT DISTINCT categorie FROM tag ORDER BY categorie")]


def tous_les_tags(conn: psycopg.Connection) -> list[dict]:
    """Tous les tags, pour la liste de choix : [{"id", "categorie", "libelle"}]."""
    return conn.execute("SELECT id, categorie, libelle FROM tag ORDER BY categorie, libelle").fetchall()


def trouver_ou_creer(conn: psycopg.Connection, categorie: str, libelle: str, par: str) -> int:
    """Identifiant du tag, créé s'il n'existe pas. « veeam » retrouve « Veeam » : la comparaison
    ignore la casse et les accents, pour ne pas créer de doublon."""
    categorie, libelle = categorie.strip().lower(), " ".join(libelle.split())
    if not categorie or not libelle:
        raise TagInvalide("Indiquez une catégorie et un libellé.")
    if len(categorie) > LIBELLE_MAX or len(libelle) > LIBELLE_MAX:
        raise TagInvalide(f"{LIBELLE_MAX} caractères au plus.")
    existant = conn.execute(
        "SELECT id FROM tag WHERE ged_normaliser(categorie) = ged_normaliser(%s) "
        "AND ged_normaliser(libelle) = ged_normaliser(%s)",
        (categorie, libelle),
    ).fetchone()
    if existant:
        return existant["id"]
    # Libellé avec majuscule, comme ceux de taxonomie.toml
    return conn.execute(
        "INSERT INTO tag (categorie, libelle, cree_par) VALUES (%s, %s, %s) "
        "ON CONFLICT (categorie, libelle) DO UPDATE SET libelle = EXCLUDED.libelle RETURNING id",
        (categorie, libelle[:1].upper() + libelle[1:], par),
    ).fetchone()["id"]


def ajouter(conn: psycopg.Connection, document_id: int, tag_id: int, par: str) -> None:
    """Pose le tag à la main : il ne dépend plus de la taxonomie ni des règles de dossier."""
    conn.execute("DELETE FROM tag_retire WHERE document_id = %s AND tag_id = %s", (document_id, tag_id))
    conn.execute(
        "INSERT INTO document_tag (document_id, tag_id, origine, pose_par, pose_le) "
        "VALUES (%s, %s, 'manuel', %s, now()) "
        "ON CONFLICT (document_id, tag_id) DO UPDATE SET origine = 'manuel', pose_par = EXCLUDED.pose_par, "
        "pose_le = EXCLUDED.pose_le",
        (document_id, tag_id, par),
    )


def retirer(conn: psycopg.Connection, document_id: int, tag_id: int, par: str) -> None:
    """Retire le tag du document, qui ne le reprendra plus de la taxonomie ni d'un dossier."""
    conn.execute("DELETE FROM document_tag WHERE document_id = %s AND tag_id = %s", (document_id, tag_id))
    conn.execute(
        "INSERT INTO tag_retire (document_id, tag_id, retire_par) VALUES (%s, %s, %s) "
        "ON CONFLICT (document_id, tag_id) DO UPDATE SET retire_par = EXCLUDED.retire_par, retire_le = now()",
        (document_id, tag_id, par),
    )


def appliquer_regles_document(conn: psycopg.Connection, document_id: int) -> None:
    """Tags des dossiers qui contiennent ce document (appelé par l'indexeur, après la taxonomie)."""
    conn.execute(
        "INSERT INTO document_tag (document_id, tag_id, origine, pose_par, pose_le) "
        "SELECT DISTINCT ON (r.tag_id) d.id, r.tag_id, 'dossier', r.pose_par, r.pose_le "
        f"FROM document d JOIN tag_dossier r ON {dans_le_dossier()} "
        "WHERE d.id = %s AND NOT EXISTS (SELECT 1 FROM tag_retire x WHERE x.document_id = d.id AND x.tag_id = r.tag_id) "
        "ON CONFLICT (document_id, tag_id) DO NOTHING",
        (document_id,),
    )


def regles_du_dossier(conn: psycopg.Connection, source_id: int, chemin: str) -> list[dict]:
    """Tags qui s'appliquent au dossier : les siens et ceux hérités d'un dossier parent
    ({"id", "tag_id", "categorie", "libelle", "chemin", "herite", "pose_par", "nb"})."""
    return conn.execute(
        "SELECT r.id, r.tag_id, t.categorie, t.libelle, r.chemin, r.pose_par, "
        "lower(r.chemin) <> lower(%(chemin)s) AS herite, "
        "(SELECT count(*) FROM document_tag dt JOIN document d ON d.id = dt.document_id "
        f" WHERE dt.tag_id = r.tag_id AND dt.origine = 'dossier' AND {dans_le_dossier()}) AS nb "
        "FROM tag_dossier r JOIN tag t ON t.id = r.tag_id "
        "WHERE r.source_id = %(source)s AND (r.chemin = '' OR lower(r.chemin) = lower(%(chemin)s) "
        "OR left(lower(%(chemin)s), length(r.chemin) + 1) = lower(r.chemin) || '\\') "
        "ORDER BY length(r.chemin), t.categorie, t.libelle",
        {"source": source_id, "chemin": chemin},
    ).fetchall()


def ajouter_au_dossier(conn: psycopg.Connection, source_id: int, chemin: str, tag_id: int, par: str) -> int:
    """Pose le tag sur le dossier et l'applique tout de suite à ses documents. Renvoie leur nombre."""
    chemin = chemin.strip("\\")
    regle = conn.execute(
        "INSERT INTO tag_dossier (source_id, chemin, tag_id, pose_par) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (source_id, chemin, tag_id) DO UPDATE SET pose_par = EXCLUDED.pose_par RETURNING id",
        (source_id, chemin, tag_id, par),
    ).fetchone()["id"]
    return conn.execute(
        "INSERT INTO document_tag (document_id, tag_id, origine, pose_par, pose_le) "
        "SELECT d.id, r.tag_id, 'dossier', r.pose_par, r.pose_le "
        f"FROM tag_dossier r JOIN document d ON {dans_le_dossier()} "
        "WHERE r.id = %s AND d.supprime_le IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM tag_retire x WHERE x.document_id = d.id AND x.tag_id = r.tag_id) "
        "ON CONFLICT (document_id, tag_id) DO NOTHING",
        (regle,),
    ).rowcount


def retirer_du_dossier(conn: psycopg.Connection, regle_id: int) -> int:
    """Retire le tag du dossier et de ses documents, sauf ceux qui le tiennent d'ailleurs (posé à
    la main, taxonomie, autre dossier parent). Renvoie le nombre de documents qui le perdent."""
    regle = conn.execute("DELETE FROM tag_dossier WHERE id = %s RETURNING source_id, chemin, tag_id", (regle_id,)).fetchone()
    if not regle:
        return 0
    return conn.execute(
        "DELETE FROM document_tag dt USING document d, (SELECT %(source)s AS source_id, %(chemin)s AS chemin) r "
        f"WHERE dt.document_id = d.id AND dt.tag_id = %(tag)s AND dt.origine = 'dossier' AND {dans_le_dossier()} "
        "AND NOT EXISTS (SELECT 1 FROM tag_dossier r2 JOIN document d2 ON d2.id = d.id "
        f" AND {dans_le_dossier('r2', 'd2')} WHERE r2.tag_id = %(tag)s)",
        {"source": regle["source_id"], "chemin": regle["chemin"], "tag": regle["tag_id"]},
    ).rowcount


def nb_poses_a_la_main(conn: psycopg.Connection) -> dict:
    """Ce que « ged reinitialiser » ferait perdre."""
    return conn.execute(
        "SELECT (SELECT count(*) FROM document_tag WHERE origine = 'manuel') AS manuels, "
        "(SELECT count(*) FROM tag_retire) AS retires, (SELECT count(*) FROM tag_dossier) AS dossiers"
    ).fetchone()
