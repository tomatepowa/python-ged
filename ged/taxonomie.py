"""Proposition automatique de tags à partir d'un dictionnaire de mots-clés (taxonomie.toml)."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ged.texte import normaliser

POIDS_NOM = 3      # une occurrence dans le nom, le titre ou le chemin
POIDS_CONTENU = 1  # une occurrence dans le texte


@dataclass(frozen=True)
class Tag:
    categorie: str
    libelle: str


@dataclass
class Taxonomie:
    regles: dict[Tag, re.Pattern]
    seuil: int = 2

    @classmethod
    def charger(cls, chemin: Path) -> "Taxonomie":
        if not chemin.is_file():
            return cls(regles={})
        with chemin.open("rb") as f:
            donnees = tomllib.load(f)
        seuil = int(donnees.pop("seuil", 2))
        regles = {}
        for categorie, entrees in donnees.items():
            if not isinstance(entrees, dict):
                continue
            for libelle, mots in entrees.items():
                alternatives = "|".join(re.escape(normaliser(m)) for m in mots if m.strip())
                if alternatives:
                    # Mot entier : "ins" ne doit pas trouver "instance"
                    regles[Tag(categorie, libelle)] = re.compile(rf"(?<![a-z0-9])(?:{alternatives})(?![a-z0-9])")
        return cls(regles=regles, seuil=seuil)

    def proposer(self, nom: str, contenu: str) -> list[Tag]:
        """Tags dont le score atteint le seuil. `nom` regroupe nom de fichier, titre et chemin."""
        nom_n = normaliser(re.sub(r"[_\-.\\/]+", " ", nom))
        contenu_n = normaliser(contenu)
        tags = []
        for tag, motif in self.regles.items():
            score = POIDS_NOM * len(motif.findall(nom_n)) + POIDS_CONTENU * len(motif.findall(contenu_n))
            if score >= self.seuil:
                tags.append(tag)
        return tags
