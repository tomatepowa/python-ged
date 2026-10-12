"""Barre de progression en console pour l'indexation et l'OCR.

Dans une console : une ligne réécrite en place, avec la barre, le rythme, le temps restant et
le fichier en cours de lecture. Sortie redirigée (tâche planifiée, fichier journal) : une ligne
de journal de temps en temps, pour ne pas noyer le journal.
"""

from __future__ import annotations

import logging
import shutil
import sys
import time

log = logging.getLogger(__name__)

RAFRAICHIR_TOUTES_LES = 0.1  # secondes, en console
JOURNAL_TOUTES_LES = 30.0  # secondes, sortie redirigée
LARGEUR_BARRE = 24

# Progression affichée en ce moment : les messages du journal l'effacent avant de s'écrire
_active: Progression | None = None


def en_console() -> bool:
    return sys.stderr.isatty()


def nombre(n: int) -> str:
    """12345 -> '12 345'."""
    return f"{n:,}".replace(",", " ")


def duree(secondes: float) -> str:
    if secondes < 60:
        return f"{secondes:.0f} s"
    if secondes < 3600:
        return f"{secondes / 60:.0f} min"
    return f"{secondes // 3600:.0f} h {secondes % 3600 / 60:02.0f}"


def _ecrire(texte: str) -> None:
    sys.stderr.write(texte)
    sys.stderr.flush()


def largeur_console() -> int:
    # Un caractère de marge : une ligne pleine passerait à la ligne suivante
    return shutil.get_terminal_size((100, 20)).columns - 1


def _ligne(texte: str) -> None:
    """Réécrit la ligne courante de la console, coupée à sa largeur."""
    largeur = largeur_console()
    _ecrire("\r" + texte[:largeur].ljust(largeur))


def _effacer() -> None:
    _ligne("")
    _ecrire("\r")


class EffacerProgression(logging.Filter):
    """Filtre à poser sur le gestionnaire du journal : efface la barre avant chaque message,
    qui s'écrit ainsi sur sa propre ligne ; la barre se redessine au fichier suivant."""

    def filter(self, record: logging.LogRecord) -> bool:
        if _active and _active.console:
            _effacer()
            _active.dernier_affichage = 0.0
        return True


def installer_filtre() -> None:
    for gestionnaire in logging.getLogger().handlers:
        gestionnaire.addFilter(EffacerProgression())


class Inventaire:
    """Compteur pendant l'inventaire des fichiers, avant de connaître le total."""

    def __init__(self, libelle: str):
        self.libelle = libelle
        self.nb = 0
        self.console = en_console()
        self.dernier_affichage = 0.0

    def __enter__(self) -> Inventaire:
        global _active
        _active = self  # type: ignore[assignment]
        return self

    def compter(self) -> None:
        self.nb += 1
        maintenant = time.monotonic()
        if self.console and maintenant - self.dernier_affichage >= RAFRAICHIR_TOUTES_LES:
            self.dernier_affichage = maintenant
            _ligne(f"  {self.libelle} : {nombre(self.nb)} fichiers…")

    def __exit__(self, *exc) -> None:
        global _active
        _active = None
        if self.console:
            _effacer()


class Progression:
    """Avancement sur un total connu. `fichier(nom)` avant de traiter un élément, `fait()` après."""

    def __init__(self, total: int, libelle: str):
        self.total = total
        self.libelle = libelle
        self.faits = 0
        self.en_cours = ""
        # Précision sur le fichier en cours (page de l'OCR), toujours visible en entier
        self.detail = ""
        self.console = en_console()
        self.debut = time.monotonic()
        self.dernier_affichage = 0.0
        self.dernier_journal = self.debut

    def __enter__(self) -> Progression:
        global _active
        _active = self
        return self

    def fichier(self, nom: str) -> None:
        """Élément dont la lecture commence (affiché tout de suite : un gros PDF peut prendre du temps)."""
        self.en_cours = nom
        self.detail = ""
        self._afficher()

    def page(self, numero: int, nb_pages: int) -> None:
        """Page en cours du fichier en cours (OCR), affichée après son nom : « (p. 3/12) »."""
        self.detail = f" (p. {numero}/{nb_pages})" if nb_pages > 1 else ""
        self._afficher()

    def fait(self) -> None:
        self.faits += 1
        self._afficher()

    def _afficher(self, forcer: bool = False) -> None:
        maintenant = time.monotonic()
        if self.console:
            if forcer or maintenant - self.dernier_affichage >= RAFRAICHIR_TOUTES_LES:
                self.dernier_affichage = maintenant
                _ligne(self.texte(maintenant, largeur_console()))
        elif maintenant - self.dernier_journal >= JOURNAL_TOUTES_LES:
            self.dernier_journal = maintenant
            log.info("  %s : %s/%s (%d %%)", self.libelle, nombre(self.faits), nombre(self.total),
                     100 * self.faits // max(self.total, 1))

    def texte(self, maintenant: float, largeur: int = 120) -> str:
        """Barre, compte, temps restant, fichier en cours. Console étroite : barre raccourcie,
        puis le fichier passe avant le rythme (c'est lui qu'on veut voir quand ça bloque)."""
        part = self.faits / self.total if self.total else 1.0
        taille_barre = LARGEUR_BARRE if largeur >= 110 else 10
        plein = round(part * taille_barre)
        debut = (f"  [{'█' * plein}{'░' * (taille_barre - plein)}] {part:4.0%}"
                 f"  {nombre(self.faits)}/{nombre(self.total)}")
        # Temps restant : après quelques secondes, sinon l'estimation est fantaisiste
        ecoule = maintenant - self.debut
        if ecoule >= 3 and self.faits:
            par_seconde = self.faits / ecoule
            reste = f"  reste ~{duree((self.total - self.faits) / par_seconde)}"
            rythme = f"  {par_seconde:.1f}/s".replace(".", ",")
            # Par ordre de priorité, tant que le nom du fichier garde au moins 20 caractères
            for morceau in (reste, rythme):
                if len(debut) + len(morceau) + 2 + min(len(self.en_cours), 20) <= largeur:
                    debut += morceau
        # Fichier en cours : abrégé par la gauche, le nom du fichier et la page restent visibles
        place = largeur - len(debut) - 2 - len(self.detail)
        if self.en_cours and place >= 12:
            nom = self.en_cours if len(self.en_cours) <= place else "…" + self.en_cours[-(place - 1):]
            return f"{debut}  {nom}{self.detail}"
        return debut + self.detail

    def __exit__(self, *exc) -> None:
        global _active
        _active = None
        if self.console:
            _effacer()
