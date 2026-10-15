"""Proposition automatique de tags à partir d'un dictionnaire de mots-clés.

La taxonomie vit en base (voir ged.taxonomie_base) ; taxonomie.toml sert d'import et d'export,
au format décrit dans EN_TETE.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ged.texte import normaliser

PREFIXE_TITRE = "titre:"
SEUIL_DEFAUT = 2
DENSITE_DEFAUT = 0.5

EN_TETE = """# Taxonomie : tags proposés automatiquement à partir du contenu des documents.
#
# Export de la base de la GED (page Tags ou « ged exporter-taxonomie ») : après modification,
# réimporter avec « ged importer-taxonomie » (les tags absents du fichier perdent leurs mots-clés).
#
# Format : "Libellé du tag" = ["mot-clé", "autre mot-clé", ...]
#   - la casse et les accents sont ignorés ("Identitovigilance" = "identitovigilance")
#   - un mot-clé est cherché en mot entier ("ad" ne trouve pas "admission")
#   - préfixe "titre:" : le mot-clé ne compte que dans le nom, le titre ou le chemin du fichier
#     (pour les mots trop courants dans un texte, comme "procédure" ou "identité")
#   - un tag sans mot-clé (= []) existe mais n'est jamais posé automatiquement
#
# Un tag est posé si un mot-clé apparaît dans le nom / titre / chemin du fichier,
# ou s'il apparaît au moins `seuil` fois dans le texte, avec au moins `densite_min`
# mentions pour 1000 mots (un long document cite tout sans en parler vraiment).
"""


@dataclass(frozen=True)
class Tag:
    categorie: str
    libelle: str


@dataclass(frozen=True)
class Regle:
    # Cherché dans le nom, le titre et le chemin : tous les mots-clés
    motif_nom: re.Pattern
    # Cherché dans le texte : les mots-clés sans le préfixe "titre:" (None s'il n'y en a pas)
    motif_contenu: re.Pattern | None


def _motif(mots: list[str]) -> re.Pattern | None:
    alternatives = "|".join(re.escape(normaliser(m)) for m in mots if m.strip())
    # Mot entier : "ins" ne doit pas trouver "instance"
    return re.compile(rf"(?<![a-z0-9])(?:{alternatives})(?![a-z0-9])") if alternatives else None


def regle(mots: list[str]) -> Regle | None:
    """Règle d'un tag d'après ses mots-clés (None s'il n'en a aucun)."""
    motif_nom = _motif([m.removeprefix(PREFIXE_TITRE) for m in mots])
    if not motif_nom:
        return None
    return Regle(motif_nom, _motif([m for m in mots if not m.startswith(PREFIXE_TITRE)]))


def sans_prefixe(mot: str) -> str:
    return mot.removeprefix(PREFIXE_TITRE)


@dataclass
class Definition:
    """La taxonomie telle qu'on l'écrit : mots-clés de chaque tag, et réglages."""

    mots: dict[Tag, list[str]] = field(default_factory=dict)
    seuil: int = SEUIL_DEFAUT
    densite_min: float = DENSITE_DEFAUT


def lire_toml(texte: str) -> Definition:
    """Définition lue au format de taxonomie.toml. ValueError si le format n'est pas respecté."""
    try:
        donnees = tomllib.loads(texte)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"TOML invalide : {exc}") from exc
    try:
        definition = Definition(seuil=int(donnees.pop("seuil", SEUIL_DEFAUT)),
                                densite_min=float(donnees.pop("densite_min", DENSITE_DEFAUT)))
    except (TypeError, ValueError) as exc:
        raise ValueError("seuil et densite_min doivent être des nombres") from exc
    for categorie, entrees in donnees.items():
        if not isinstance(entrees, dict):
            continue
        for libelle, mots in entrees.items():
            if not isinstance(mots, list) or not all(isinstance(m, str) for m in mots):
                raise ValueError(f"[{categorie}] « {libelle} » : une liste de mots-clés entre guillemets est attendue")
            definition.mots[Tag(categorie, libelle)] = mots
    return definition


def _cle_toml(cle: str) -> str:
    return cle if re.fullmatch(r"[A-Za-z0-9_-]+", cle) else json.dumps(cle, ensure_ascii=False)


def en_toml(definition: Definition) -> str:
    """Texte au format de taxonomie.toml (catégories et tags triés), relisible par lire_toml."""
    lignes = [EN_TETE, f"seuil = {definition.seuil}", f"densite_min = {definition.densite_min}"]
    par_categorie: dict[str, list[Tag]] = {}
    for tag in definition.mots:
        par_categorie.setdefault(tag.categorie, []).append(tag)
    for categorie in sorted(par_categorie, key=normaliser):
        lignes += ["", f"[{_cle_toml(categorie)}]"]
        for tag in sorted(par_categorie[categorie], key=lambda t: normaliser(t.libelle)):
            # json.dumps : chaînes au format TOML (guillemets et barres obliques inverses échappés)
            mots = ", ".join(json.dumps(m, ensure_ascii=False) for m in definition.mots[tag])
            lignes.append(f"{json.dumps(tag.libelle, ensure_ascii=False)} = [{mots}]")
    return "\n".join(lignes) + "\n"


@dataclass
class Taxonomie:
    regles: dict[Tag, Regle]
    # Nombre minimal de mentions dans le texte...
    seuil: int = SEUIL_DEFAUT
    # ... et densité minimale (mentions pour 1000 mots) : un document de 50 pages cite forcément
    # "procédure" ou "identité" quelque part sans en parler pour autant.
    densite_min: float = DENSITE_DEFAUT

    @classmethod
    def construire(cls, definition: Definition) -> Taxonomie:
        regles = {tag: r for tag, mots in definition.mots.items() if (r := regle(mots))}
        return cls(regles=regles, seuil=definition.seuil, densite_min=definition.densite_min)

    @classmethod
    def charger(cls, chemin: Path) -> Taxonomie:
        """Taxonomie d'un fichier au format de taxonomie.toml (vide s'il n'existe pas)."""
        if not chemin.is_file():
            return cls(regles={})
        return cls.construire(lire_toml(chemin.read_text(encoding="utf-8")))

    def proposer(self, nom: str, contenu: str) -> list[Tag]:
        """Tags proposés. `nom` regroupe nom de fichier, titre et chemin.

        Un tag est posé si un mot-clé apparaît dans le nom, ou s'il apparaît au moins `seuil` fois
        dans le texte avec une densité d'au moins `densite_min` mentions pour 1000 mots.
        """
        nom_n = normaliser(re.sub(r"[_\-.\\/]+", " ", nom))
        contenu_n = normaliser(contenu)
        milliers_de_mots = max(len(contenu_n.split()), 1) / 1000
        tags = []
        for tag, regle in self.regles.items():
            if regle.motif_nom.search(nom_n):
                tags.append(tag)
                continue
            if regle.motif_contenu is None:
                continue
            mentions = len(regle.motif_contenu.findall(contenu_n))
            if mentions >= self.seuil and mentions / milliers_de_mots >= self.densite_min:
                tags.append(tag)
        return tags

    def expliquer(self, tag: Tag, nom: str, contenu: str) -> str:
        """Pourquoi `proposer` pose (ou non) ce tag : mot trouvé dans le nom, ou mentions dans le texte."""
        regle = self.regles.get(tag)
        if regle is None:
            return "aucun mot-clé"
        if trouve := regle.motif_nom.search(normaliser(re.sub(r"[_\-.\\/]+", " ", nom))):
            return f"« {trouve.group(0)} » dans le nom"
        if regle.motif_contenu is None:
            return "absent du nom"
        mentions = regle.motif_contenu.findall(normaliser(contenu))
        if not mentions:
            return "absent du nom et du texte"
        return f"{len(mentions)} mention{'s' if len(mentions) > 1 else ''} dans le texte (« {mentions[0]} »)"
