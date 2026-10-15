"""Recalcul des tags en arrière-plan pour la page Tags, et verrou partagé avec « ged recalculer ».

Un seul recalcul à la fois (verrou consultatif PostgreSQL, valable aussi entre processus). Les
demandes faites pendant un recalcul attendent leur tour ; un recalcul complet en attente rend
inutiles les recalculs d'un tag demandés avant lui.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager

import psycopg

from ged.taxonomie_base import Avancement, recalculer_tag, recalculer_tous

log = logging.getLogger(__name__)

# Clé du verrou consultatif (« ged_ » en ASCII)
CLE_VERROU = 0x6765645F


class RecalculEnCours(RuntimeError):
    pass


@contextmanager
def verrou_recalcul(conn: psycopg.Connection):
    """Empêche deux recalculs simultanés (page Tags, « ged recalculer »). RecalculEnCours si pris."""
    if not conn.execute("SELECT pg_try_advisory_lock(%s) AS libre", (CLE_VERROU,)).fetchone()["libre"]:
        conn.rollback()
        raise RecalculEnCours("Un recalcul des tags est déjà en cours (page Tags ou « ged recalculer »).")
    try:
        yield
    finally:
        # Verrou de session : il survit à l'annulation de la transaction, qu'il faut d'abord clore
        conn.rollback()
        conn.execute("SELECT pg_advisory_unlock(%s)", (CLE_VERROU,))
        conn.commit()


def executer(conn: psycopg.Connection, tag_id: int | None, avancement: Avancement) -> str:
    """Recalcule un tag (ou tous si `tag_id` est None) sous le verrou. Renvoie le bilan à afficher."""
    with verrou_recalcul(conn):
        if tag_id is None:
            bilan = recalculer_tous(conn, avancement)
            return f"{bilan['examines']} documents recalculés."
        bilan = recalculer_tag(conn, tag_id, avancement)
        return f"{bilan['examines']} documents examinés : tag posé sur {bilan['poses']}, retiré de {bilan['retraits']}."


class RecalculEnArrierePlan:
    """File des recalculs demandés depuis la page, traitée par un fil d'exécution à part."""

    def __init__(self, connecter: Callable[[], psycopg.Connection],
                 executer: Callable[[psycopg.Connection, int | None, Avancement], str] = executer):
        self._connecter = connecter
        self._executer = executer
        self._verrou = threading.Lock()
        self._file: list[tuple[int | None, str]] = []
        self._fil: threading.Thread | None = None
        self._etat = {"en_cours": False, "libelle": "", "fait": 0, "total": 0, "message": "", "erreur": ""}

    def demander(self, tag_id: int | None, libelle: str) -> None:
        """Ajoute un recalcul à la file : `tag_id` None pour tous les tags."""
        with self._verrou:
            if any(t is None for t, _ in self._file):
                pass  # un recalcul complet attend déjà : il couvrira celui-ci
            elif tag_id is None:
                self._file = [(None, libelle)]
            elif all(t != tag_id for t, _ in self._file):
                self._file.append((tag_id, libelle))
            # Le fil s'efface lui-même (sous le verrou) quand la file est vide
            if self._fil is None:
                self._fil = threading.Thread(target=self._traiter, name="recalcul-tags", daemon=True)
                self._fil.start()

    def etat(self) -> dict:
        with self._verrou:
            return {**self._etat, "en_attente": [libelle for _, libelle in self._file]}

    def attendre(self, delai: float = 10) -> None:
        """Attend la fin des recalculs en file (tests)."""
        fin = time.monotonic() + delai
        while (fil := self._fil) is not None and time.monotonic() < fin:
            fil.join(0.05)

    def _avancer(self, fait: int, total: int) -> None:
        with self._verrou:
            self._etat.update(fait=fait, total=total)

    def _traiter(self) -> None:
        while True:
            with self._verrou:
                if not self._file:
                    self._etat["en_cours"] = False
                    self._fil = None
                    return
                tag_id, libelle = self._file.pop(0)
                self._etat.update(en_cours=True, libelle=libelle, fait=0, total=0, erreur="")
            try:
                with self._connecter() as conn:
                    message = self._executer(conn, tag_id, self._avancer)
                with self._verrou:
                    self._etat["message"] = f"Recalcul de {libelle} terminé : {message}"
            except Exception as exc:  # base injoignable, verrou pris par « ged recalculer »...
                log.exception("Recalcul des tags (%s) en échec", libelle)
                with self._verrou:
                    self._etat.update(message="", erreur=f"Recalcul de {libelle} impossible : {exc}")
