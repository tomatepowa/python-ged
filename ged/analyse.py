"""Rapport pour construire la taxonomie : `ged analyser`.

Statistiques calculées localement à partir de l'index, sans lire le contenu des documents :
noms de dossiers, mots des noms de fichiers et des titres, couverture des tags actuels.
Le rapport (Markdown) sert à compléter la taxonomie (page Tags, ou export taxonomie.toml), ou à le faire avec un
assistant IA si la politique du groupe l'autorise : il ne contient ni texte de document, ni
chemin complet, et seulement des termes présents dans au moins `min_docs` documents.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import pairwise

import psycopg

from ged.taxonomie import Taxonomie
from ged.texte import normaliser

MIN_DOCS = 3
NIVEAUX_DOSSIERS = 3
LIGNES_MAX = 60

# Mots sans intérêt pour un tag : mots vides, extensions, mots de version et de copie
MOTS_VIDES = set("""
a au aux avec ce ces cette dans de des du en et for la le les leur mais ne ou par pas
pour qui que sur the to un une vers via of and is on at by son sa ses nos notre vos votre
pdf doc docx xls xlsx xlsm ppt pptx txt csv msg eml jpg jpeg png tif tiff zip rar odt ods
copie copy version vf final finale def definitif new nouveau nouvelle old ancien ancienne
bis ter temp tmp test draft brouillon doc fichier document documents divers autre autres
""".split())
_MOT = re.compile(r"[a-z][a-z0-9']*[a-z0-9]|[a-z]")


@dataclass
class Document:
    chemin_relatif: str
    nom: str
    titre: str | None
    nb_tags: int


def mots(texte: str) -> list[str]:
    """Mots utiles d'un nom de fichier ou d'un titre, normalisés (minuscules, sans accents)."""
    texte = normaliser(re.sub(r"[_\-.()\[\]]+", " ", texte)).replace("œ", "oe").replace("æ", "ae")
    return [m for m in _MOT.findall(texte)
            if len(m) >= 3 and m not in MOTS_VIDES
            and not re.fullmatch(r"v?\d+[a-z]?", m)  # numéros, versions
            and not (re.search(r"\d", m) and re.fullmatch(r"[0-9a-f]+", m))]  # morceaux d'identifiants (GUID)


def nom_de_dossier_utile(nom: str) -> bool:
    """Écarte les dossiers qui ne nomment rien : dates, numéros, années."""
    return bool(re.search(r"[A-Za-zÀ-ÿ]{3}", nom))


def couvert(taxonomie: Taxonomie, terme: str) -> str | None:
    """Tag de la taxonomie dont un mot-clé reconnaît ce terme, sinon None."""
    for tag, regle in taxonomie.regles.items():
        if regle.motif_nom.search(normaliser(terme)):
            return f"{tag.categorie} / {tag.libelle}"
    return None


def _tableau(entetes: list[str], lignes: list[list]) -> list[str]:
    sortie = ["| " + " | ".join(entetes) + " |", "|" + "|".join("---" for _ in entetes) + "|"]
    sortie += ["| " + " | ".join(str(c).replace("|", "/") for c in ligne) + " |" for ligne in lignes]
    return sortie


def _pourcent(n: int, total: int) -> str:
    return f"{100 * n / total:.0f} %" if total else "–"


def rapport(documents: list[Document], taxonomie: Taxonomie, comptes_tags: dict[tuple[str, str], int],
            min_docs: int = MIN_DOCS) -> str:
    total = len(documents)
    tagues = sum(1 for d in documents if d.nb_tags)
    sortie = [
        "# Analyse pour la taxonomie",
        "",
        "Statistiques calculées sur l'index de la GED, sans le texte des documents : noms de "
        f"dossiers et mots des noms de fichiers / titres présents dans au moins {min_docs} documents.",
        "",
        "> **À relire avant de partager** : des noms de personnes, de clients ou de projets peuvent "
        "apparaître dans les noms de dossiers ou de fichiers. Supprimez-les du fichier si besoin.",
        "",
        "## Vue d'ensemble",
        "",
        f"- {total} documents, dont {tagues} avec au moins un tag ({_pourcent(tagues, total)}) "
        f"et {total - tagues} sans tag.",
        f"- {len(taxonomie.regles)} tags avec des mots-clés dans la taxonomie.",
        "",
        "## Couverture des tags actuels",
        "",
        "Nombre de documents portant chaque tag. Un tag à 0 a des mots-clés qui ne correspondent à rien.",
        "",
    ]
    lignes = sorted(
        ([tag.categorie, tag.libelle, comptes_tags.get((tag.categorie, tag.libelle), 0)] for tag in taxonomie.regles),
        key=lambda ligne: (ligne[0], -ligne[2]),
    )
    sortie += _tableau(["Catégorie", "Tag", "Documents"], lignes) + [""]

    # Dossiers, par niveau sous la source : nombre de documents et part déjà taguée
    par_dossier: dict[tuple[int, str], list[int]] = defaultdict(lambda: [0, 0])
    recurrents: dict[str, set[str]] = defaultdict(set)
    docs_par_nom_dossier: Counter = Counter()
    for d in documents:
        parties = d.chemin_relatif.split("\\")[:-1]
        for niveau, nom in enumerate(parties, start=1):
            if not nom_de_dossier_utile(nom):
                continue
            if niveau <= NIVEAUX_DOSSIERS:
                compte = par_dossier[(niveau, nom)]
                compte[0] += 1
                compte[1] += bool(d.nb_tags)
            cle = normaliser(nom)
            recurrents[cle].add("\\".join(parties[:niveau - 1]))
            docs_par_nom_dossier[cle] += 1

    sortie += ["## Dossiers par niveau", "",
               "Les noms de dossiers d'une GED de DSI désignent souvent un logiciel, un secteur ou un "
               "type de document : de bons candidats pour des tags.", ""]
    for niveau in range(1, NIVEAUX_DOSSIERS + 1):
        lignes = [[nom, n, _pourcent(t, n)] for (niv, nom), (n, t) in par_dossier.items()
                  if niv == niveau and n >= min_docs]
        if not lignes:
            continue
        lignes.sort(key=lambda ligne: -ligne[1])
        sortie += [f"### Niveau {niveau}", ""]
        sortie += _tableau(["Dossier", "Documents", "Déjà tagués"], lignes[:LIGNES_MAX]) + [""]

    lignes = [[nom, len(lieux), docs_par_nom_dossier[nom], couvert(taxonomie, nom) or ""]
              for nom, lieux in recurrents.items() if len(lieux) >= 2 and docs_par_nom_dossier[nom] >= min_docs]
    lignes.sort(key=lambda ligne: (-ligne[1], -ligne[2]))
    if lignes:
        sortie += ["## Noms de dossiers récurrents", "",
                   "Le même nom de dossier à plusieurs endroits de l'arborescence : souvent un thème transversal.",
                   ""]
        sortie += _tableau(["Nom", "Emplacements", "Documents", "Déjà couvert par"], lignes[:LIGNES_MAX]) + [""]

    # Mots et paires de mots des noms de fichiers et des titres (fréquence en documents)
    frequence_mots: Counter = Counter()
    frequence_paires: Counter = Counter()
    for d in documents:
        liste = mots(f"{d.nom.rsplit('.', 1)[0]} {d.titre or ''}")
        frequence_mots.update(set(liste))
        frequence_paires.update({f"{a} {b}" for a, b in pairwise(liste) if a != b})

    termes = [(t, n) for t, n in frequence_mots.most_common() if n >= min_docs]
    paires = [(t, n) for t, n in frequence_paires.most_common() if n >= min_docs]
    sortie += ["## Mots des noms de fichiers et des titres", ""]
    sortie += _tableau(["Mot", "Documents", "Déjà couvert par"],
                       [[t, n, couvert(taxonomie, t) or ""] for t, n in termes[:LIGNES_MAX]]) + [""]
    if paires:
        sortie += ["## Expressions de deux mots", ""]
        sortie += _tableau(["Expression", "Documents", "Déjà couvert par"],
                           [[t, n, couvert(taxonomie, t) or ""] for t, n in paires[:LIGNES_MAX // 2]]) + [""]

    # Dossiers les moins bien couverts : là où la taxonomie manque le plus
    lignes = [[f"niveau {niv} : {nom}", n, _pourcent(t, n)] for (niv, nom), (n, t) in par_dossier.items()
              if n >= max(min_docs, 5) and t / n < 0.5]
    lignes.sort(key=lambda ligne: -ligne[1])
    if lignes:
        sortie += ["## Dossiers peu couverts", "", "Moins de la moitié des documents y ont un tag.", ""]
        sortie += _tableau(["Dossier", "Documents", "Déjà tagués"], lignes[:LIGNES_MAX // 2]) + [""]

    # Propositions brutes, au format de taxonomie.toml, à trier à la main
    candidats = [(t, n) for t, n in termes + paires if not couvert(taxonomie, t)]
    candidats.sort(key=lambda c: -c[1])
    sortie += ["## Propositions à trier", "",
               "Termes fréquents qu'aucun tag ne reconnaît. Ajouter les utiles comme mots-clés d'un tag "
               "dans la page Tags, ou dans l'export taxonomie.toml (`[logiciel]`, `[secteur]`, `[type]`…) "
               "à réimporter avec `ged importer-taxonomie`.", "",
               "```toml"]
    sortie += [f'# "{t.capitalize()}" = ["{t}"]    # {n} documents' for t, n in candidats[:LIGNES_MAX]]
    sortie += ["```", ""]
    return "\n".join(sortie)


def charger(conn: psycopg.Connection) -> tuple[list[Document], dict[tuple[str, str], int]]:
    documents = [
        Document(**ligne) for ligne in conn.execute(
            "SELECT d.chemin_relatif, d.nom, d.titre, "
            "(SELECT count(*) FROM document_tag dt WHERE dt.document_id = d.id)::int AS nb_tags "
            "FROM document d WHERE d.supprime_le IS NULL"
        )
    ]
    comptes = {
        (ligne["categorie"], ligne["libelle"]): ligne["nb"] for ligne in conn.execute(
            "SELECT t.categorie, t.libelle, count(d.id)::int AS nb FROM tag t "
            "JOIN document_tag dt ON dt.tag_id = t.id JOIN document d ON d.id = dt.document_id "
            "WHERE d.supprime_le IS NULL GROUP BY t.id"
        )
    }
    return documents, comptes
