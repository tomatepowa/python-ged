"""Vue « explorateur » : arborescence des dossiers et contenu d'un dossier.

Tout est reconstruit depuis l'index (chemins relatifs des documents) : le NAS n'est pas relu
à l'affichage, et seuls les dossiers contenant des documents indexés apparaissent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PureWindowsPath

import psycopg

SEPARATEUR = "\\"
TRIS = {
    "nom": lambda f: f["nom"].lower(),
    "date": lambda f: f["modifie_le"],
    "taille": lambda f: f["taille"],
    "type": lambda f: (f["famille"], f["nom"].lower()),
}


@dataclass
class Dossier:
    nom: str
    # Chemin relatif à la source, "" pour la racine, séparateur "\"
    chemin: str
    # Documents dans ce dossier ET ses sous-dossiers
    nb_documents: int = 0
    derniere_modification: datetime | None = None
    enfants: dict[str, "Dossier"] = field(default_factory=dict)

    def sous_dossiers(self) -> list["Dossier"]:
        return sorted(self.enfants.values(), key=lambda d: d.nom.lower())


def dossier_parent(chemin_relatif: str) -> str:
    """'a\\b\\fichier.pdf' -> 'a\\b' ; 'fichier.pdf' -> ''."""
    parent = str(PureWindowsPath(chemin_relatif).parent)
    return "" if parent == "." else parent


def construire_arbre(documents: list[tuple[str, datetime]]) -> Dossier:
    """Arbre des dossiers à partir des (chemin relatif, date de modification) des documents."""
    racine = Dossier(nom="", chemin="")
    for chemin_relatif, modifie_le in documents:
        noeud = racine
        noeud.nb_documents += 1
        noeud.derniere_modification = max(filter(None, [noeud.derniere_modification, modifie_le]), default=None)
        parties = dossier_parent(chemin_relatif).split(SEPARATEUR) if dossier_parent(chemin_relatif) else []
        for i, partie in enumerate(parties):
            if partie not in noeud.enfants:
                noeud.enfants[partie] = Dossier(nom=partie, chemin=SEPARATEUR.join(parties[: i + 1]))
            noeud = noeud.enfants[partie]
            noeud.nb_documents += 1
            noeud.derniere_modification = max(filter(None, [noeud.derniere_modification, modifie_le]), default=None)
    return racine


def trouver(racine: Dossier, chemin: str) -> Dossier | None:
    noeud = racine
    for partie in filter(None, chemin.split(SEPARATEUR)):
        noeud = noeud.enfants.get(partie)
        if noeud is None:
            return None
    return noeud


def fil_ariane(chemin: str) -> list[tuple[str, str]]:
    """'a\\b' -> [('a', 'a'), ('b', 'a\\b')]."""
    parties = [p for p in chemin.split(SEPARATEUR) if p]
    return [(partie, SEPARATEUR.join(parties[: i + 1])) for i, partie in enumerate(parties)]


def sources(conn: psycopg.Connection) -> list[dict]:
    """Sources avec leur arbre de dossiers."""
    resultat = []
    for source in conn.execute("SELECT id, nom, pole, chemin FROM source ORDER BY nom").fetchall():
        documents = conn.execute(
            "SELECT chemin_relatif, modifie_le FROM document WHERE source_id = %s AND supprime_le IS NULL",
            (source["id"],),
        ).fetchall()
        source["arbre"] = construire_arbre([(d["chemin_relatif"], d["modifie_le"]) for d in documents])
        resultat.append(source)
    return resultat


def fichiers(conn: psycopg.Connection, source_id: int, chemin: str, tri: str = "nom", inverse: bool = False) -> list[dict]:
    """Documents situés directement dans le dossier (pas dans ses sous-dossiers)."""
    prefixe = chemin + SEPARATEUR if chemin else ""
    lignes = conn.execute(
        """
        SELECT d.id, d.nom, d.famille, d.extension, d.taille, d.modifie_le, d.statut, d.code_doc,
               d.auteur, d.dernier_modificateur, d.proprietaire,
               coalesce((SELECT json_agg(json_build_object('id', t.id, 'libelle', t.libelle) ORDER BY t.libelle)
                         FROM document_tag dt JOIN tag t ON t.id = dt.tag_id
                         WHERE dt.document_id = d.id), '[]') AS tags
        FROM document d
        WHERE d.source_id = %(source)s AND d.supprime_le IS NULL
          AND starts_with(d.chemin_relatif, %(prefixe)s)
          AND strpos(substr(d.chemin_relatif, length(%(prefixe)s) + 1), %(separateur)s) = 0
        """,
        {"source": source_id, "prefixe": prefixe, "separateur": SEPARATEUR},
    ).fetchall()
    return sorted(lignes, key=TRIS.get(tri, TRIS["nom"]), reverse=inverse)
