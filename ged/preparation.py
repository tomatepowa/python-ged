"""Préparation de l'indexation : parcours rapide des sources et choix des dossiers à exclure.

Le parcours ne lit que les noms, tailles et dates (aucun fichier n'est ouvert) : quelques minutes
même sur un gros partage. Il applique les mêmes règles que l'indexeur (dossiers système, programmes,
fichiers sensibles), si bien que les nombres affichés sont ceux que l'indexation traitera.

Les dossiers décochés sont enregistrés dans les réglages (exclure_prepares, voir ged.parametres).
"""

from __future__ import annotations

import glob
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from ged.config import Config
from ged.indexeur import EXTENSIONS_TECHNIQUES, dossier_exclu, est_ignore
from ged.texte import normaliser

log = logging.getLogger(__name__)

# Sauvegardes, images disque et boîtes mail : volumineux et sans intérêt pour la recherche
EXTENSIONS_SAUVEGARDE = {
    ".bak", ".bkf", ".vbk", ".vib", ".vrb", ".tib", ".vhd", ".vhdx", ".vmdk", ".iso", ".img", ".wim",
    ".pst", ".ost", ".dump", ".dmp",
}
# Mots de noms de dossiers qui contiennent rarement de la documentation (sans accents, au singulier)
NOMS_SUSPECTS = {
    "install", "installation", "installeur", "setup", "logiciel", "driver", "pilote", "sauvegarde",
    "backup", "node_modules", "cache", "temp", "tmp", "corbeille", "old",
}
# Au-delà, le dossier est signalé comme « rien de modifié depuis N ans »
ANS_SANS_MODIFICATION = 5


@dataclass
class Dossier:
    """Un dossier parcouru et les totaux de tout ce qu'il contient (sous-dossiers compris)."""

    id: int
    chemin: Path
    nom: str
    enfants: list[Dossier] = field(default_factory=list)
    fichiers: int = 0          # fichiers que l'indexation traitera
    taille: int = 0            # leur volume, en octets
    recent: float = 0          # date de modification la plus récente (horodatage)
    programmes: int = 0        # programmes et bibliothèques, écartés d'office
    sauvegardes: int = 0       # volume des sauvegardes et images disque, en octets
    sous_dossiers: int = 0
    inaccessible: bool = False

    def ajouter(self, enfant: Dossier) -> None:
        self.enfants.append(enfant)
        self.fichiers += enfant.fichiers
        self.taille += enfant.taille
        self.recent = max(self.recent, enfant.recent)
        self.programmes += enfant.programmes
        self.sauvegardes += enfant.sauvegardes
        self.sous_dossiers += 1 + enfant.sous_dossiers
        self.inaccessible = self.inaccessible or enfant.inaccessible

    def tous(self):
        """Ce dossier et tous ses sous-dossiers."""
        yield self
        for enfant in self.enfants:
            yield from enfant.tous()


def parcourir_rapide(racine: Path, signaler=None) -> Dossier:
    """Arborescence de `racine` avec ses totaux. `signaler(nb_dossiers)` : avancement."""
    compteur = [0]

    def visiter(chemin: Path) -> Dossier:
        compteur[0] += 1
        if signaler:
            signaler(compteur[0])
        dossier = Dossier(compteur[0], chemin, chemin.name or str(chemin))
        try:
            entrees = sorted(os.scandir(chemin), key=lambda e: e.name.lower())
        except OSError as exc:
            log.warning("Inaccessible : %s (%s)", chemin, exc.strerror)
            dossier.inaccessible = True
            return dossier
        for entree in entrees:
            try:
                if entree.is_dir(follow_symlinks=False):
                    # Seuls les dossiers système sont sautés : ceux déjà exclus restent affichés
                    if not dossier_exclu(Path(entree.path), []):
                        dossier.ajouter(visiter(Path(entree.path)))
                    continue
                fichier = Path(entree.path)
                if fichier.suffix.lower() in EXTENSIONS_TECHNIQUES:
                    dossier.programmes += 1
                if est_ignore(fichier, []):
                    continue
                infos = entree.stat()
            except OSError:
                continue
            if fichier.suffix.lower() in EXTENSIONS_SAUVEGARDE:
                dossier.sauvegardes += infos.st_size
            dossier.fichiers += 1
            dossier.taille += infos.st_size
            dossier.recent = max(dossier.recent, infos.st_mtime)
        return dossier

    return visiter(racine)


def parcourir_sources(cfg: Config) -> list[tuple[str, Dossier]]:
    """(nom de la source, arborescence) pour chaque source, avec l'avancement dans la console."""
    resultat = []
    for source in cfg.sources:
        debut, dernier = time.monotonic(), [0.0]

        def signaler(nb: int, nom=source.nom, debut=debut, dernier=dernier) -> None:
            if time.monotonic() - dernier[0] >= 2:
                dernier[0] = time.monotonic()
                print(f"  {nom} : {nb} dossiers parcourus ({time.monotonic() - debut:.0f} s)", flush=True)

        print(f"Parcours de {source.nom} ({source.chemin})...", flush=True)
        arbre = parcourir_rapide(source.chemin, signaler)
        print(f"  {arbre.sous_dossiers} dossiers, {arbre.fichiers} fichiers ({time.monotonic() - debut:.0f} s)")
        resultat.append((source.nom, arbre))
    # Identifiants uniques sur toutes les sources (ils servent de valeur aux cases à cocher)
    numero = 0
    for _, arbre in resultat:
        for dossier in arbre.tous():
            numero += 1
            dossier.id = numero
    return resultat


def alertes(dossier: Dossier, maintenant: float | None = None) -> dict[str, str]:
    """Raisons de se demander s'il faut garder ce dossier (affichées, jamais décochées d'office) :
    {genre: texte}, le genre servant à ne pas répéter dans un sous-dossier l'alerte de son parent."""
    raisons = {}
    nom = normaliser(dossier.nom)
    mots = {nom, *re.split(r"[^a-z0-9]+", nom)}
    if any(mot in NOMS_SUSPECTS or mot.removesuffix("s") in NOMS_SUSPECTS for mot in mots):
        raisons["nom"] = "nom de dossier technique"
    if dossier.programmes >= 20 and dossier.programmes >= dossier.fichiers * 0.3:
        raisons["programmes"] = f"{dossier.programmes} programmes : dossier d'application ?"
    if dossier.taille and dossier.sauvegardes >= dossier.taille * 0.5:
        raisons["sauvegardes"] = "surtout des sauvegardes ou images disque"
    if dossier.recent:
        ans = ((maintenant or time.time()) - dossier.recent) / (365.25 * 24 * 3600)
        if ans >= ANS_SANS_MODIFICATION:
            raisons["ancien"] = f"rien de modifié depuis {int(ans)} ans"
    return raisons


def fichiers_indexes(dossier: Dossier, motifs: list[str]) -> int:
    """Fichiers du dossier que l'indexation lira avec ces exclusions."""
    if dossier_exclu(dossier.chemin, motifs):
        return 0
    propres = dossier.fichiers - sum(e.fichiers for e in dossier.enfants)
    return propres + sum(fichiers_indexes(e, motifs) for e in dossier.enfants)


def motif_exact(chemin: Path) -> str:
    """Motif d'exclusion qui ne vise que ce dossier (crochets, * et ? du nom neutralisés)."""
    return glob.escape(str(chemin))


def exclusions_minimales(decoches: list[Path]) -> list[Path]:
    """Dossiers décochés sans ceux déjà couverts par un dossier parent décoché."""
    gardes: list[Path] = []
    for chemin in sorted(decoches, key=lambda c: len(c.parts)):
        if not any(chemin.is_relative_to(parent) for parent in gardes):
            gardes.append(chemin)
    return sorted(gardes, key=lambda c: str(c).lower())


def motifs_exclusions(dossiers: list[Dossier], conserves: list[str] = ()) -> list[str]:
    """Motifs enregistrés dans les réglages : un par dossier exclu.

    `conserves` : motifs précédents qui ne visent aucun dossier parcouru (source retirée,
    dossier inaccessible ce jour-là...) : gardés tels quels plutôt que perdus.
    """
    return [motif_exact(d.chemin) for d in dossiers] + list(conserves)


def motifs_orphelins(motifs: list[str], sources: list[tuple[str, Dossier]]) -> list[str]:
    """Motifs qui ne visent aucun des dossiers parcourus."""
    chemins = [d.chemin for _, arbre in sources for d in arbre.tous()]
    return [m for m in motifs if not any(dossier_exclu(c, [m]) for c in chemins)]
