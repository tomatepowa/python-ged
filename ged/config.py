"""Configuration de la GED.

Deux étages :
- connexion.toml (écrit par l'assistant d'installation, non versionné) : l'adresse de la base, et
  l'adresse d'écoute du serveur, nécessaires avant de pouvoir lire quoi que ce soit en base ;
- tous les autres réglages, en base (table parametre, voir ged.parametres), modifiables par
  l'assistant puis par la page Réglages des administrateurs.

Le fichier est cherché dans $GED_CONNEXION, sinon ./connexion.toml.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

FICHIER_CONNEXION = "connexion.toml"
HOTE_DEFAUT = "127.0.0.1"
PORT_DEFAUT = 8000


class NonConfiguree(Exception):
    """La GED n'a pas encore été installée (pas de connexion.toml, ou assistant pas terminé)."""


@dataclass(frozen=True)
class Source:
    nom: str
    chemin: Path
    pole: str


@dataclass(frozen=True)
class Connexion:
    url: str
    hote: str = HOTE_DEFAUT
    port: int = PORT_DEFAUT


@dataclass(frozen=True)
class Config:
    url_base: str
    sources: list[Source]
    hote: str = HOTE_DEFAUT
    port: int = PORT_DEFAUT
    taille_max_mo: int = 100
    # Motifs d'exclusion appliqués : ceux écrits dans les réglages, puis ceux de « ged preparer »
    exclure: list[str] = field(default_factory=list)
    # Ceux écrits dans les réglages seuls (« ged preparer » ne peut pas les retirer)
    exclure_manuels: list[str] = field(default_factory=list)
    # Ceux choisis avec « ged preparer » (dossiers décochés)
    exclure_prepares: list[str] = field(default_factory=list)
    ocr_active: bool = False
    tessdata: str | None = None
    langue_ocr: str = "fra"
    dpi_ocr: int = 300
    soffice: str | None = None
    # Fichier lu et écrit par « ged importer-taxonomie » / « ged exporter-taxonomie »
    taxonomie: Path = Path("taxonomie.toml")
    # Exemples affichés dans la barre de recherche vide : « Rechercher : <exemples>… »
    exemples_recherche: str = ""
    # Authentification Windows et filtrage des documents selon les droits NTFS (ged.acces)
    authentification: bool = True
    # "NTLM" ou "Negotiate" (Kerberos, si le compte du serveur porte le SPN HTTP/<serveur>)
    protocole: str = "NTLM"
    # Groupes (« DOMAINE\groupe » ou SID) qui voient tous les documents et règlent la GED
    groupes_admin: list[str] = field(default_factory=list)
    duree_session_heures: float = 8
    # Dépôt de nouveaux documents (ged.depot) : noms des sources où la GED peut ranger les
    # documents déposés (les seules où elle écrit) ; vide : dépôt désactivé
    sources_depot: list[str] = field(default_factory=list)
    # Zone d'attente : fichiers déposés, pas encore classés (hors des sources indexées)
    attente_depot: Path = Path("depot")
    taille_max_depot_mo: int = 50

    def sources_du_depot(self) -> list[Source]:
        return [s for s in self.sources if s.nom in self.sources_depot]

    def source_du_depot(self, nom: str) -> Source | None:
        """Source où la GED peut ranger un document déposé (None : inconnue ou pas ouverte au dépôt)."""
        return next((s for s in self.sources_du_depot() if s.nom == nom), None)


# --- connexion.toml ---------------------------------------------------------------------------


def fichier_connexion(chemin: str | Path | None = None) -> Path:
    return Path(chemin or os.environ.get("GED_CONNEXION") or FICHIER_CONNEXION)


def lire_connexion(chemin: str | Path | None = None) -> Connexion | None:
    """Connexion lue dans connexion.toml (None s'il n'existe pas encore)."""
    fichier = fichier_connexion(chemin)
    if not fichier.is_file():
        return None
    try:
        with fichier.open("rb") as f:
            donnees = tomllib.load(f)
        url = donnees["base"]["url"]
    except (tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        raise SystemExit(f"{fichier.resolve()} illisible ({exc}) : relancez l'assistant (ged serveur --assistant).") from exc
    serveur = donnees.get("serveur", {})
    return Connexion(url=url, hote=str(serveur.get("hote", HOTE_DEFAUT)), port=int(serveur.get("port", PORT_DEFAUT)))


def ecrire_connexion(connexion: Connexion, chemin: str | Path | None = None) -> Path:
    fichier = fichier_connexion(chemin)
    fichier.parent.mkdir(parents=True, exist_ok=True)
    # json.dumps : chaîne entre guillemets, échappée comme TOML l'attend
    fichier.write_text(
        "# Connexion de la GED à sa base, écrite par l'assistant d'installation "
        f"le {datetime.now():%d/%m/%Y à %H:%M}.\n"
        "# Non versionné : contient le mot de passe de la base. Les autres réglages sont en base\n"
        "# (page Réglages, ou « ged serveur --assistant »).\n\n"
        f"[base]\nurl = {json.dumps(connexion.url)}\n\n"
        f"[serveur]\nhote = {json.dumps(connexion.hote)}\nport = {connexion.port}\n",
        encoding="utf-8",
    )
    return fichier


# --- Chargement complet -------------------------------------------------------------------------


def charger(chemin: str | Path | None = None) -> Config:
    """Configuration complète : connexion.toml, puis réglages en base (schéma mis à jour au passage).

    Lève NonConfiguree si la GED n'est pas encore installée.
    """
    from ged import parametres
    from ged.db import connecter, initialiser

    connexion = lire_connexion(chemin)
    if connexion is None:
        raise NonConfiguree(
            f"GED pas encore installée ({fichier_connexion(chemin).resolve()} absent) : lancez « ged serveur » "
            "puis ouvrez l'interface sur ce poste pour suivre l'assistant d'installation."
        )
    initialiser(connexion.url)
    with connecter(connexion.url) as conn:
        if not parametres.est_installee(conn):
            raise NonConfiguree(
                "Installation pas terminée : lancez « ged serveur » puis ouvrez l'interface sur ce poste "
                "pour suivre l'assistant d'installation."
            )
        return parametres.config(connexion, parametres.lire(conn))
