"""Arborescence des résultats de recherche et outils sur les chemins.

Tout est reconstruit depuis l'index (chemins relatifs des documents) : le NAS n'est pas relu
à l'affichage, et seuls les dossiers contenant des documents trouvés apparaissent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PureWindowsPath

SEPARATEUR = "\\"


@dataclass
class Dossier:
    nom: str
    # Chemin relatif à la source, "" pour la racine, séparateur "\"
    chemin: str
    # Documents dans ce dossier ET ses sous-dossiers
    nb_documents: int = 0
    enfants: dict[str, Dossier] = field(default_factory=dict)
    # Documents situés directement dans ce dossier (vue arborescence des résultats)
    fichiers: list[dict] = field(default_factory=list)

    def sous_dossiers(self) -> list[Dossier]:
        return sorted(self.enfants.values(), key=lambda d: d.nom.lower())


def dossier_parent(chemin_relatif: str) -> str:
    """'a\\b\\fichier.pdf' -> 'a\\b' ; 'fichier.pdf' -> ''."""
    parent = str(PureWindowsPath(chemin_relatif).parent)
    return "" if parent == "." else parent


@dataclass
class Branche:
    """Dossier tel qu'affiché dans l'arbre des résultats."""
    # Les dossiers qui ne contiennent qu'un sous-dossier sont fusionnés : "Documents › iCloud › Famille"
    nom: str
    # Chemin du dossier le plus profond de la chaîne fusionnée
    chemin: str
    nb_documents: int
    fichiers: list[dict]
    enfants: list[Branche]
    # Dossiers de la chaîne fusionnée, sauf le dernier : (nom, chemin), chacun cliquable
    ancetres: list[tuple[str, str]] = field(default_factory=list)


def construire_arbre_resultats(documents: list[dict]) -> list[dict]:
    """Arbre des seuls documents trouvés, par source.

    `documents` : lignes avec source_id, source, chemin_relatif, nom (cf. rechercher(tous=True)).
    """
    racines: dict[int, tuple[str, Dossier]] = {}
    for doc in documents:
        nom_source, racine = racines.setdefault(doc["source_id"], (doc["source"], Dossier(nom="", chemin="")))
        noeud = racine
        noeud.nb_documents += 1
        parent = dossier_parent(doc["chemin_relatif"])
        parties = parent.split(SEPARATEUR) if parent else []
        for i, partie in enumerate(parties):
            if partie not in noeud.enfants:
                noeud.enfants[partie] = Dossier(nom=partie, chemin=SEPARATEUR.join(parties[: i + 1]))
            noeud = noeud.enfants[partie]
            noeud.nb_documents += 1
        noeud.fichiers.append(doc)

    return [
        {
            "source_id": source_id,
            "source": nom_source,
            "nb_documents": racine.nb_documents,
            "fichiers": sorted(racine.fichiers, key=lambda f: f["nom"].lower()),
            "enfants": [_compacter(e) for e in racine.sous_dossiers()],
        }
        for source_id, (nom_source, racine) in sorted(racines.items(), key=lambda r: r[1][0].lower())
    ]


def _compacter(noeud: Dossier) -> Branche:
    noms = [noeud.nom]
    ancetres = []
    while not noeud.fichiers and len(noeud.enfants) == 1:
        ancetres.append((noeud.nom, noeud.chemin))
        noeud = next(iter(noeud.enfants.values()))
        noms.append(noeud.nom)
    return Branche(
        nom=" › ".join(noms),
        ancetres=ancetres,
        chemin=noeud.chemin,
        nb_documents=noeud.nb_documents,
        fichiers=sorted(noeud.fichiers, key=lambda f: f["nom"].lower()),
        enfants=[_compacter(e) for e in noeud.sous_dossiers()],
    )


def fil_ariane(chemin: str) -> list[tuple[str, str]]:
    """'a\\b' -> [('a', 'a'), ('b', 'a\\b')]."""
    parties = [p for p in chemin.split(SEPARATEUR) if p]
    return [(partie, SEPARATEUR.join(parties[: i + 1])) for i, partie in enumerate(parties)]


