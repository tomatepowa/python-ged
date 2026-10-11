"""Lecture du fichier config.toml."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Source:
    nom: str
    chemin: Path
    pole: str


@dataclass(frozen=True)
class Config:
    url_base: str
    sources: list[Source]
    hote: str = "127.0.0.1"
    port: int = 8000
    taille_max_mo: int = 100
    exclure: list[str] = field(default_factory=list)
    ocr_active: bool = False
    tessdata: str | None = None
    langue_ocr: str = "fra"
    dpi_ocr: int = 300
    soffice: str | None = None
    taxonomie: Path = Path("taxonomie.toml")
    # Exemples affichés dans la barre de recherche vide : « Rechercher : <exemples>… »
    exemples_recherche: str = ""


def charger(chemin: str | Path | None = None) -> Config:
    """Charge la configuration depuis `chemin`, $GED_CONFIG ou ./config.toml."""
    fichier = Path(chemin or os.environ.get("GED_CONFIG", "config.toml"))
    if not fichier.is_file():
        raise SystemExit(
            f"Configuration introuvable : {fichier.resolve()}\n"
            "Copiez config.exemple.toml en config.toml et adaptez-le."
        )
    with fichier.open("rb") as f:
        donnees = tomllib.load(f)

    base = donnees.get("base", {})
    serveur = donnees.get("serveur", {})
    indexation = donnees.get("indexation", {})
    ocr = donnees.get("ocr", {})
    conversion = donnees.get("conversion", {})
    interface = donnees.get("interface", {})

    if "url" not in base:
        raise SystemExit(f"{fichier} : la clé [base] url est obligatoire.")

    sources = [
        Source(nom=s["nom"], chemin=Path(s["chemin"]), pole=s.get("pole", s["nom"]))
        for s in donnees.get("sources", [])
    ]

    return Config(
        url_base=base["url"],
        sources=sources,
        hote=serveur.get("hote", "127.0.0.1"),
        port=int(serveur.get("port", 8000)),
        taille_max_mo=int(indexation.get("taille_max_mo", 100)),
        exclure=list(indexation.get("exclure", [])),
        ocr_active=bool(ocr.get("active", False)),
        tessdata=ocr.get("tessdata"),
        langue_ocr=ocr.get("langue", "fra"),
        dpi_ocr=int(ocr.get("dpi", 300)),
        soffice=conversion.get("soffice"),
        taxonomie=fichier.parent / donnees.get("taxonomie", "taxonomie.toml"),
        exemples_recherche=str(interface.get("exemples_recherche", "")).strip(),
    )
