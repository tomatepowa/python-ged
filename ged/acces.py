"""Qui peut voir quoi : filtrage des documents selon les droits Windows de la personne connectée.

Un document est visible si la personne peut lire le fichier d'après ses droits NTFS (ged.droits,
relus à chaque indexation) et, quand ils ont pu être lus, d'après les droits du partage réseau
de la source. Comme sous Windows, la première entrée de la liste des droits qui concerne l'un de
ses groupes décide : un refus l'emporte sur une autorisation qui le suit.

Un document dont les droits n'ont pas pu être lus est masqué. Les administrateurs de la GED
(groupes de la configuration) voient tout.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg


@dataclass(frozen=True)
class Lecteur:
    """La personne connectée."""
    compte: str
    # SID de son compte et de ses groupes actifs : ils comptent pour les autorisations...
    sids: frozenset[str]
    # ... et, pour les refus, s'y ajoutent les groupes « refus seulement » de son jeton Windows
    sids_refus: frozenset[str]
    admin: bool = False
    # SID du compte lui-même (propriétaire des fichiers, nom complet dans l'annuaire)
    sid: str = ""

    @classmethod
    def sans_filtre(cls) -> Lecteur:
        """Authentification désactivée : tout est visible."""
        return cls(compte="", sids=frozenset(), sids_refus=frozenset(), admin=True)


def peut_lire(acl: list[list[str]], sids: frozenset[str], sids_refus: frozenset[str]) -> bool:
    """`acl` : [[SID, "A" (autorisé) ou "R" (refusé)], ...] dans l'ordre de Windows.

    Même règle que la fonction SQL ged_peut_lire (schema.sql).
    """
    for sid, sens in acl:
        if sens == "A" and sid in sids:
            return True
        if sens == "R" and sid in sids_refus:
            return False
    return False


@dataclass(frozen=True)
class FiltreAcces:
    """Condition SQL sur la table document (alias `d`) et ses paramètres nommés."""
    sql: str
    parametres: dict


TOUT = FiltreAcces("TRUE", {})


def filtre_acces(conn: psycopg.Connection, lecteur: Lecteur) -> FiltreAcces:
    """Condition qui ne garde que les documents que `lecteur` a le droit de lire."""
    if lecteur.admin:
        return TOUT
    # Droits des partages : une ligne par source, évalués ici plutôt que pour chaque document
    sources = [
        s["id"] for s in conn.execute("SELECT id, acces_partage FROM source")
        if s["acces_partage"] is None or peut_lire(s["acces_partage"], lecteur.sids, lecteur.sids_refus)
    ]
    return FiltreAcces(
        "(d.source_id = ANY(%(acces_sources)s::int[]) AND d.acces_sids && %(acces_sids)s::text[] "
        "AND (d.acces_ordre IS NULL OR ged_peut_lire(d.acces_ordre, %(acces_sids)s::text[], %(acces_refus)s::text[])))",
        {"acces_sources": sources, "acces_sids": sorted(lecteur.sids), "acces_refus": sorted(lecteur.sids_refus)},
    )


def diagnostic_acces(conn: psycopg.Connection) -> str:
    """Pourquoi une personne ne voit aucun document.

    "index_vide" : rien n'est indexé ; "droits_absents" : les droits des fichiers n'ont pas encore
    été enregistrés (première indexation depuis la mise en place du filtrage) ; "aucun_droit" :
    ses droits Windows ne lui permettent de lire aucun document indexé.
    """
    comptes = conn.execute(
        "SELECT count(*) AS total, count(acces_sids) AS avec_droits FROM document WHERE supprime_le IS NULL"
    ).fetchone()
    if not comptes["total"]:
        return "index_vide"
    if not comptes["avec_droits"]:
        return "droits_absents"
    return "aucun_droit"


def sids_accordant(acces_sids: list[str] | None, acces_ordre: list[list[str]] | None, lecteur: Lecteur) -> list[str]:
    """SID de la personne (compte ou groupes) qui lui donnent la lecture d'un document.

    Avec un refus dans la liste des droits, seules comptent les autorisations placées avant le
    premier refus qui la concerne (Windows s'arrête à la première entrée qui décide).
    """
    if acces_ordre is None:
        return [sid for sid in acces_sids or [] if sid in lecteur.sids]
    accordant: list[str] = []
    for sid, sens in acces_ordre:
        if sens == "R" and sid in lecteur.sids_refus:
            break
        if sens == "A" and sid in lecteur.sids and sid not in accordant:
            accordant.append(sid)
    return accordant
