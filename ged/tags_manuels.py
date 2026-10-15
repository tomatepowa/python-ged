"""Tags posés à la main, et gestion des tags par les administrateurs.

Sur un document, tout utilisateur qui le voit peut :
- ajouter un tag existant (origine « manuel ») : il survit aux recalculs ;
- retirer les tags qu'il a lui-même posés (le tag revient s'il découle aussi de la taxonomie ou
  d'un dossier : l'utilisateur annule son ajout, il ne corrige pas la taxonomie).

Les administrateurs de la GED, en plus :
- retirent n'importe quel tag, définitivement (noté dans tag_retire : ni la taxonomie ni un
  dossier ne le remettent) ;
- posent un tag sur un dossier (table tag_dossier) : tous ses documents le portent (origine
  « dossier »), sous-dossiers compris, y compris ceux qui y arriveront plus tard ;
- créent, renomment, fusionnent et suppriment les tags (page Tags).

Chaque action est notée dans journal_tag. Les mots-clés de la taxonomie sont attachés au tag
(voir ged.taxonomie_base) : ils le suivent quand il est renommé, passent au tag cible d'une
fusion et disparaissent avec lui.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import psycopg

if TYPE_CHECKING:
    from ged.acces import Lecteur
    from ged.taxonomie import Taxonomie

LIBELLE_MAX = 60
JOURNAL_MAX = 300


class TagInvalide(ValueError):
    pass


class RetraitRefuse(PermissionError):
    pass


def dans_le_dossier(r: str = "r", d: str = "d") -> str:
    """Condition SQL « le document `d` est dans le dossier de la règle `r` » (source_id, chemin),
    sous-dossiers compris. Sans casse, comme Windows ; pas de LIKE, que « _ » et « % » fausseraient."""
    return (
        f"{d}.source_id = {r}.source_id AND ({r}.chemin = '' "
        f"OR left(lower({d}.chemin_relatif), length({r}.chemin) + 1) = lower({r}.chemin) || '\\')"
    )


def auteur(lecteur: Lecteur) -> str:
    """Nom noté dans le journal et sur les tags posés (« ? » sans identification)."""
    return lecteur.compte or "?"


def peut_retirer(lecteur: Lecteur, pose: dict) -> bool:
    """Un administrateur retire n'importe quel tag ; un utilisateur, ceux qu'il a posés à la main."""
    if lecteur.admin:
        return True
    return pose["origine"] == "manuel" and bool(lecteur.sid) and pose.get("pose_par_sid") == lecteur.sid


def journaliser(conn: psycopg.Connection, par: str, action: str, tag_id: int | None = None,
                document_id: int | None = None, detail: str | None = None) -> None:
    """Note l'action, avec le libellé du tag et le chemin du document tels qu'ils sont à cet instant."""
    conn.execute(
        "INSERT INTO journal_tag (par, action, tag_id, tag, document_id, document, detail) VALUES ("
        "%(par)s, %(action)s, %(tag)s, (SELECT libelle || ' (' || categorie || ')' FROM tag WHERE id = %(tag)s), "
        "%(doc)s, (SELECT chemin_relatif FROM document WHERE id = %(doc)s), %(detail)s)",
        {"par": par, "action": action, "tag": tag_id, "doc": document_id, "detail": detail},
    )


# --- Liste des tags -------------------------------------------------------------------------


def categories(conn: psycopg.Connection) -> list[str]:
    return [ligne["categorie"] for ligne in conn.execute("SELECT DISTINCT categorie FROM tag ORDER BY categorie")]


def tous_les_tags(conn: psycopg.Connection) -> list[dict]:
    """Tous les tags, pour les listes de choix : [{"id", "categorie", "libelle"}]."""
    return conn.execute("SELECT id, categorie, libelle FROM tag ORDER BY categorie, libelle").fetchall()


def id_tag(conn: psycopg.Connection, categorie: str, libelle: str) -> int:
    """Identifiant d'un tag, créé au besoin (indexation, recalcul, import de la taxonomie)."""
    return conn.execute(
        "INSERT INTO tag (categorie, libelle) VALUES (%s, %s) "
        "ON CONFLICT (categorie, libelle) DO UPDATE SET libelle = EXCLUDED.libelle RETURNING id",
        (categorie, libelle),
    ).fetchone()["id"]


def _nettoyer(categorie: str, libelle: str) -> tuple[str, str]:
    """Catégorie en minuscules, libellé avec majuscule (comme taxonomie.toml), espaces réduits."""
    categorie, libelle = " ".join(categorie.split()).lower(), " ".join(libelle.split())
    if not categorie or not libelle:
        raise TagInvalide("Indiquez une catégorie et un libellé.")
    if len(categorie) > LIBELLE_MAX or len(libelle) > LIBELLE_MAX:
        raise TagInvalide(f"{LIBELLE_MAX} caractères au plus.")
    return categorie, libelle[:1].upper() + libelle[1:]


def homonyme(conn: psycopg.Connection, categorie: str, libelle: str, sauf: int | None = None) -> dict | None:
    """Tag de même nom, sans tenir compte de la casse ni des accents (« veeam » = « Véeam »)."""
    return conn.execute(
        "SELECT id, categorie, libelle, mots_cles FROM tag WHERE ged_normaliser(categorie) = ged_normaliser(%s) "
        "AND ged_normaliser(libelle) = ged_normaliser(%s) AND id IS DISTINCT FROM %s",
        (categorie, libelle, sauf),
    ).fetchone()


# --- Sur un document ------------------------------------------------------------------------


def reappliquer(conn: psycopg.Connection, document_id: int, tags_proposes: list[int]) -> None:
    """Remplace les tags de la taxonomie (`tags_proposes`) et des dossiers ; ceux posés à la main
    sont conservés, et ceux retirés par un administrateur ne reviennent pas."""
    conn.execute("DELETE FROM document_tag WHERE document_id = %s AND origine IN ('auto', 'dossier')", (document_id,))
    for tag in tags_proposes:
        conn.execute(
            "INSERT INTO document_tag (document_id, tag_id, origine) SELECT %(doc)s, %(tag)s, 'auto' "
            "WHERE NOT EXISTS (SELECT 1 FROM tag_retire WHERE document_id = %(doc)s AND tag_id = %(tag)s) "
            "ON CONFLICT DO NOTHING",
            {"doc": document_id, "tag": tag},
        )
    conn.execute(
        "INSERT INTO document_tag (document_id, tag_id, origine, pose_par, pose_le) "
        "SELECT DISTINCT ON (r.tag_id) d.id, r.tag_id, 'dossier', r.pose_par, r.pose_le "
        f"FROM document d JOIN tag_dossier r ON {dans_le_dossier()} "
        "WHERE d.id = %s AND NOT EXISTS (SELECT 1 FROM tag_retire x WHERE x.document_id = d.id AND x.tag_id = r.tag_id) "
        "ON CONFLICT (document_id, tag_id) DO NOTHING",
        (document_id,),
    )


def proposes_par_la_taxonomie(conn: psycopg.Connection, taxonomie: Taxonomie, document_id: int) -> list[int]:
    """Tags que la taxonomie pose sur ce document, d'après son texte déjà en base."""
    doc = conn.execute(
        "SELECT titre, chemin_relatif, contenu FROM document WHERE id = %s", (document_id,)
    ).fetchone()
    if not doc:
        return []
    tags = taxonomie.proposer(f"{doc['titre'] or ''} {doc['chemin_relatif']}", doc["contenu"] or "")
    return [id_tag(conn, t.categorie, t.libelle) for t in tags]


def ajouter(conn: psycopg.Connection, document_id: int, tag_id: int, lecteur: Lecteur) -> None:
    """Pose un tag existant à la main : il ne dépend plus de la taxonomie ni des dossiers.
    Déjà posé à la main par quelqu'un d'autre : il reste à son nom."""
    if not conn.execute("SELECT 1 FROM tag WHERE id = %s", (tag_id,)).fetchone():
        raise TagInvalide("Ce tag n'existe plus.")
    conn.execute("DELETE FROM tag_retire WHERE document_id = %s AND tag_id = %s", (document_id, tag_id))
    conn.execute(
        "INSERT INTO document_tag (document_id, tag_id, origine, pose_par, pose_par_sid, pose_le) "
        "VALUES (%s, %s, 'manuel', %s, %s, now()) "
        "ON CONFLICT (document_id, tag_id) DO UPDATE SET origine = 'manuel', pose_par = EXCLUDED.pose_par, "
        "pose_par_sid = EXCLUDED.pose_par_sid, pose_le = EXCLUDED.pose_le WHERE document_tag.origine <> 'manuel'",
        (document_id, tag_id, auteur(lecteur), lecteur.sid or None),
    )
    journaliser(conn, auteur(lecteur), "ajout", tag_id, document_id)


def retirer(conn: psycopg.Connection, document_id: int, tag_id: int, lecteur: Lecteur, taxonomie: Taxonomie) -> None:
    """Retire un tag du document (voir peut_retirer).

    Un administrateur le retire pour de bon. Un utilisateur annule seulement sa pose : si la
    taxonomie ou un dossier donnent aussi ce tag au document, il le garde à ce titre.
    """
    pose = conn.execute(
        "SELECT origine, pose_par_sid FROM document_tag WHERE document_id = %s AND tag_id = %s", (document_id, tag_id)
    ).fetchone()
    if not pose:
        return
    if not peut_retirer(lecteur, pose):
        raise RetraitRefuse("Vous ne pouvez retirer que les tags que vous avez posés.")
    conn.execute("DELETE FROM document_tag WHERE document_id = %s AND tag_id = %s", (document_id, tag_id))
    if lecteur.admin:
        conn.execute(
            "INSERT INTO tag_retire (document_id, tag_id, retire_par) VALUES (%s, %s, %s) "
            "ON CONFLICT (document_id, tag_id) DO UPDATE SET retire_par = EXCLUDED.retire_par, retire_le = now()",
            (document_id, tag_id, auteur(lecteur)),
        )
    else:
        reappliquer(conn, document_id, proposes_par_la_taxonomie(conn, taxonomie, document_id))
    journaliser(conn, auteur(lecteur), "retrait", tag_id, document_id,
                f"origine : {pose['origine']}" + ("" if lecteur.admin else " (annulation de sa pose)"))


# --- Sur un dossier (administrateurs) -------------------------------------------------------


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


def ajouter_au_dossier(conn: psycopg.Connection, source_id: int, chemin: str, tag_id: int, lecteur: Lecteur) -> int:
    """Pose le tag sur le dossier et l'applique tout de suite à ses documents. Renvoie leur nombre."""
    chemin = chemin.strip("\\")
    regle = conn.execute(
        "INSERT INTO tag_dossier (source_id, chemin, tag_id, pose_par) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (source_id, chemin, tag_id) DO UPDATE SET pose_par = EXCLUDED.pose_par RETURNING id",
        (source_id, chemin, tag_id, auteur(lecteur)),
    ).fetchone()["id"]
    nb = conn.execute(
        "INSERT INTO document_tag (document_id, tag_id, origine, pose_par, pose_le) "
        "SELECT d.id, r.tag_id, 'dossier', r.pose_par, r.pose_le "
        f"FROM tag_dossier r JOIN document d ON {dans_le_dossier()} "
        "WHERE r.id = %s AND d.supprime_le IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM tag_retire x WHERE x.document_id = d.id AND x.tag_id = r.tag_id) "
        "ON CONFLICT (document_id, tag_id) DO NOTHING",
        (regle,),
    ).rowcount
    journaliser(conn, auteur(lecteur), "ajout au dossier", tag_id, detail=f"{chemin or 'racine'} : {nb} documents")
    return nb


def retirer_du_dossier(conn: psycopg.Connection, regle_id: int, lecteur: Lecteur) -> int:
    """Retire le tag du dossier et de ses documents, sauf ceux qui le tiennent d'ailleurs (posé à
    la main, taxonomie, autre dossier parent). Renvoie le nombre de documents qui le perdent."""
    regle = conn.execute("DELETE FROM tag_dossier WHERE id = %s RETURNING source_id, chemin, tag_id", (regle_id,)).fetchone()
    if not regle:
        return 0
    nb = conn.execute(
        "DELETE FROM document_tag dt USING document d, (SELECT %(source)s AS source_id, %(chemin)s AS chemin) r "
        f"WHERE dt.document_id = d.id AND dt.tag_id = %(tag)s AND dt.origine = 'dossier' AND {dans_le_dossier()} "
        "AND NOT EXISTS (SELECT 1 FROM tag_dossier r2 JOIN document d2 ON d2.id = d.id "
        f" AND {dans_le_dossier('r2', 'd2')} WHERE r2.tag_id = %(tag)s)",
        {"source": regle["source_id"], "chemin": regle["chemin"], "tag": regle["tag_id"]},
    ).rowcount
    journaliser(conn, auteur(lecteur), "retrait du dossier", regle["tag_id"],
                detail=f"{regle['chemin'] or 'racine'} : {nb} documents")
    return nb


# --- Gestion des tags (administrateurs) -----------------------------------------------------


def tags_administres(conn: psycopg.Connection) -> list[dict]:
    """Tous les tags, leurs mots-clés et leur usage : documents par origine, dossiers, création."""
    return conn.execute(
        "SELECT t.id, t.categorie, t.libelle, t.cree_par, t.cree_le, t.mots_cles, "
        "count(dt.document_id) FILTER (WHERE dt.origine = 'auto') AS nb_auto, "
        "count(dt.document_id) FILTER (WHERE dt.origine = 'dossier') AS nb_dossier, "
        "count(dt.document_id) FILTER (WHERE dt.origine = 'manuel') AS nb_manuel, "
        "(SELECT count(*) FROM tag_dossier r WHERE r.tag_id = t.id) AS nb_dossiers, "
        "(SELECT count(*) FROM tag_retire x WHERE x.tag_id = t.id) AS nb_retires "
        "FROM tag t LEFT JOIN document_tag dt ON dt.tag_id = t.id "
        "GROUP BY t.id ORDER BY t.categorie, t.libelle"
    ).fetchall()


def creer(conn: psycopg.Connection, categorie: str, libelle: str, lecteur: Lecteur) -> int:
    categorie, libelle = _nettoyer(categorie, libelle)
    if existant := homonyme(conn, categorie, libelle):
        raise TagInvalide(f"Le tag « {existant['libelle']} » ({existant['categorie']}) existe déjà.")
    tag = conn.execute(
        "INSERT INTO tag (categorie, libelle, cree_par, cree_le) VALUES (%s, %s, %s, now()) RETURNING id",
        (categorie, libelle, auteur(lecteur)),
    ).fetchone()["id"]
    journaliser(conn, auteur(lecteur), "création", tag)
    return tag


def renommer(conn: psycopg.Connection, tag_id: int, categorie: str, libelle: str, lecteur: Lecteur) -> None:
    categorie, libelle = _nettoyer(categorie, libelle)
    if existant := homonyme(conn, categorie, libelle, sauf=tag_id):
        raise TagInvalide(f"« {existant['libelle']} » ({existant['categorie']}) existe déjà : fusionnez plutôt.")
    ancien = conn.execute("SELECT libelle || ' (' || categorie || ')' AS nom FROM tag WHERE id = %s", (tag_id,)).fetchone()
    if not ancien:
        raise TagInvalide("Ce tag n'existe plus.")
    conn.execute("UPDATE tag SET categorie = %s, libelle = %s WHERE id = %s", (categorie, libelle, tag_id))
    journaliser(conn, auteur(lecteur), "renommage", tag_id, detail=f"ancien nom : {ancien['nom']}")


def fusionner(conn: psycopg.Connection, source_id: int, cible_id: int, lecteur: Lecteur) -> int:
    """Reporte tout ce qui porte le tag `source_id` sur `cible_id`, puis supprime `source_id`.
    Renvoie le nombre de documents concernés."""
    if source_id == cible_id:
        raise TagInvalide("Choisissez un autre tag que lui-même.")
    noms = {t["id"]: t["nom"] for t in conn.execute(
        "SELECT id, libelle || ' (' || categorie || ')' AS nom FROM tag WHERE id IN (%s, %s)", (source_id, cible_id)
    )}
    if len(noms) != 2:
        raise TagInvalide("Ce tag n'existe plus.")
    # Une pose à la main ou par un dossier l'emporte sur une proposition de la taxonomie
    nb = conn.execute(
        "INSERT INTO document_tag (document_id, tag_id, origine, pose_par, pose_par_sid, pose_le) "
        "SELECT document_id, %(cible)s, origine, pose_par, pose_par_sid, pose_le FROM document_tag WHERE tag_id = %(source)s "
        "ON CONFLICT (document_id, tag_id) DO UPDATE SET origine = EXCLUDED.origine, pose_par = EXCLUDED.pose_par, "
        "pose_par_sid = EXCLUDED.pose_par_sid, pose_le = EXCLUDED.pose_le "
        "WHERE document_tag.origine = 'auto' AND EXCLUDED.origine <> 'auto'",
        {"source": source_id, "cible": cible_id},
    ).rowcount
    conn.execute(
        "INSERT INTO tag_dossier (source_id, chemin, tag_id, pose_par, pose_le) "
        "SELECT source_id, chemin, %(cible)s, pose_par, pose_le FROM tag_dossier WHERE tag_id = %(source)s "
        "ON CONFLICT DO NOTHING",
        {"source": source_id, "cible": cible_id},
    )
    conn.execute(
        "INSERT INTO tag_retire (document_id, tag_id, retire_par, retire_le) "
        "SELECT document_id, %(cible)s, retire_par, retire_le FROM tag_retire WHERE tag_id = %(source)s "
        "ON CONFLICT DO NOTHING",
        {"source": source_id, "cible": cible_id},
    )
    # Mots-clés de la taxonomie : ceux du tag fusionné s'ajoutent à ceux de la cible
    conn.execute(
        "UPDATE tag c SET mots_cles = (SELECT array_agg(m ORDER BY rang) FROM ("
        "  SELECT DISTINCT ON (ged_normaliser(m)) m, rang FROM unnest(coalesce(c.mots_cles, '{}') || s.mots_cles) "
        "  WITH ORDINALITY AS u(m, rang) ORDER BY ged_normaliser(m), rang) AS mots) "
        "FROM tag s WHERE c.id = %(cible)s AND s.id = %(source)s AND cardinality(s.mots_cles) > 0",
        {"source": source_id, "cible": cible_id},
    )
    journaliser(conn, auteur(lecteur), "fusion", cible_id, detail=f"{noms[source_id]} fusionné dedans ({nb} documents)")
    conn.execute("DELETE FROM tag WHERE id = %s", (source_id,))
    return nb


def supprimer(conn: psycopg.Connection, tag_id: int, lecteur: Lecteur) -> int:
    """Supprime le tag, ses poses, ses dossiers et ses retraits. Renvoie le nombre de documents."""
    nb = conn.execute("SELECT count(*) AS nb FROM document_tag WHERE tag_id = %s", (tag_id,)).fetchone()["nb"]
    journaliser(conn, auteur(lecteur), "suppression", tag_id, detail=f"retiré de {nb} documents")
    conn.execute("DELETE FROM tag WHERE id = %s", (tag_id,))
    return nb


def journal(conn: psycopg.Connection, par: str = "", tag_id: int | None = None) -> list[dict]:
    """Dernières actions, éventuellement d'une personne ou sur un tag."""
    return conn.execute(
        "SELECT j.*, d.id IS NOT NULL AND d.supprime_le IS NULL AS document_existe FROM journal_tag j "
        "LEFT JOIN document d ON d.id = j.document_id "
        "WHERE (%(par)s = '' OR j.par = %(par)s) AND (%(tag)s::int IS NULL OR j.tag_id = %(tag)s) "
        "ORDER BY j.le DESC, j.id DESC LIMIT %(limite)s",
        {"par": par, "tag": tag_id, "limite": JOURNAL_MAX},
    ).fetchall()


def personnes_du_journal(conn: psycopg.Connection) -> list[str]:
    return [ligne["par"] for ligne in conn.execute("SELECT DISTINCT par FROM journal_tag ORDER BY par")]


def nb_poses_a_la_main(conn: psycopg.Connection) -> dict:
    """Ce que « ged reinitialiser » ferait perdre."""
    return conn.execute(
        "SELECT (SELECT count(*) FROM document_tag WHERE origine = 'manuel') AS manuels, "
        "(SELECT count(*) FROM tag_retire) AS retires, (SELECT count(*) FROM tag_dossier) AS dossiers"
    ).fetchone()
