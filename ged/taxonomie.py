"""Proposition automatique de tags à partir d'un dictionnaire de mots-clés (taxonomie.toml)."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ged.texte import normaliser

PREFIXE_TITRE = "titre:"


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


@dataclass
class Taxonomie:
    regles: dict[Tag, Regle]
    # Nombre minimal de mentions dans le texte...
    seuil: int = 2
    # ... et densité minimale (mentions pour 1000 mots) : un document de 50 pages cite forcément
    # "procédure" ou "identité" quelque part sans en parler pour autant.
    densite_min: float = 0.5

    @classmethod
    def charger(cls, chemin: Path) -> Taxonomie:
        if not chemin.is_file():
            return cls(regles={})
        with chemin.open("rb") as f:
            donnees = tomllib.load(f)
        seuil = int(donnees.pop("seuil", 2))
        densite_min = float(donnees.pop("densite_min", 0.5))
        regles = {}
        for categorie, entrees in donnees.items():
            if not isinstance(entrees, dict):
                continue
            for libelle, mots in entrees.items():
                tous = [m.removeprefix(PREFIXE_TITRE) for m in mots]
                texte = [m for m in mots if not m.startswith(PREFIXE_TITRE)]
                motif_nom = _motif(tous)
                if motif_nom:
                    regles[Tag(categorie, libelle)] = Regle(motif_nom, _motif(texte))
        return cls(regles=regles, seuil=seuil, densite_min=densite_min)

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
