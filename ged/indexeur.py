"""Parcours des sources et mise à jour de l'index.

Garanties : les fichiers sources sont uniquement LUS. Rien n'est écrit dans les dossiers indexés.
Un fichier inchangé (même taille, même date de modification) n'est pas relu.
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
from collections import Counter
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import psycopg

from ged.config import Config, Source
from ged.extraction import Resultat, extraire, famille
from ged.ocr import ocr, texte_exploitable
from ged.taxonomie import Taxonomie
from ged.texte import detecter_code

log = logging.getLogger(__name__)

# Dossiers techniques : corbeilles et vignettes de NAS, instantanés, historiques de synchronisation
DOSSIERS_SYSTEME = {
    "#recycle", "@eadir", "@recycle", ".@__thumb", "#snapshot", "~snapshot", ".snapshot",
    ".stversions", ".stfolder", ".git", "$recycle.bin", "system volume information",
}
NOMS_BRUIT = {"thumbs.db", "desktop.ini"}
EXTENSIONS_BRUIT = {".tmp", ".lnk", ".url"}
# Jamais indexés : coffres de mots de passe, clés privées, certificats
EXTENSIONS_SENSIBLES = {".kdbx", ".kdb", ".kbdx", ".pfx", ".p12", ".key", ".pem", ".ppk"}

COMMIT_TOUS_LES = 50
MESSAGE_SANS_TEXTE = "OCR : aucun texte lisible (photo, dessin, schéma ?)"


def est_ignore(chemin: Path, exclusions: list[str]) -> bool:
    nom = chemin.name.lower()
    # "~$" : fichiers temporaires Office ; "." : fichiers techniques cachés (.stignore, .DS_Store...)
    if nom.startswith(("~$", ".")) or nom in NOMS_BRUIT:
        return True
    ext = chemin.suffix.lower()
    if ext in EXTENSIONS_BRUIT or ext in EXTENSIONS_SENSIBLES:
        return True
    return any(fnmatch.fnmatch(str(chemin), motif) for motif in exclusions)


def parcourir(racine: Path, exclusions: list[str]) -> Iterator[Path]:
    """Fichiers indexables sous `racine`, en sautant dossiers système, bruit et fichiers sensibles."""

    def signaler(erreur: OSError) -> None:
        log.warning("Inaccessible : %s (%s)", erreur.filename, erreur.strerror)

    for dossier, sous_dossiers, fichiers in os.walk(racine, onerror=signaler):
        sous_dossiers[:] = sorted(d for d in sous_dossiers if d.lower() not in DOSSIERS_SYSTEME)
        for nom in sorted(fichiers):
            chemin = Path(dossier, nom)
            if not est_ignore(chemin, exclusions):
                yield chemin


def empreinte(chemin: Path) -> str:
    h = hashlib.sha256()
    with chemin.open("rb") as f:
        for bloc in iter(lambda: f.read(1024 * 1024), b""):
            h.update(bloc)
    return h.hexdigest()


def rafraichir_vocabulaire(conn: psycopg.Connection) -> int:
    """Recalcule la liste des mots du corpus.

    Seuls les documents lus directement comptent : le texte OCR pourrait y introduire son propre
    bruit, qui serait ensuite considéré comme des mots valides.
    """
    conn.execute("TRUNCATE vocabulaire")
    conn.execute(
        """
        INSERT INTO vocabulaire (mot, nb_docs)
        SELECT word, ndoc FROM ts_stat($$
            SELECT to_tsvector('simple', unaccent(lower(
                coalesce(titre, '') || ' ' || regexp_replace(nom, '[._\\-]+', ' ', 'g') || ' '
                || left(coalesce(contenu, ''), 200000))))
            FROM document WHERE statut = 'indexe' AND methode IN ('texte', 'conversion') AND supprime_le IS NULL
        $$)
        WHERE length(word) BETWEEN 3 AND 40 AND word ~ '^[a-z]+$'
        """
    )
    nb = conn.execute("SELECT count(*) AS nb FROM vocabulaire").fetchone()["nb"]
    conn.commit()
    return nb


class Indexeur:
    def __init__(self, cfg: Config, conn: psycopg.Connection):
        self.cfg = cfg
        self.conn = conn
        self.taxonomie = Taxonomie.charger(cfg.taxonomie)
        self._ids_tags: dict[tuple[str, str], int] = {}

    # --- Indexation ---------------------------------------------------------------

    def indexer(self, forcer: bool = False, noms_sources: list[str] | None = None) -> Counter:
        bilan: Counter = Counter()
        for source in self.cfg.sources:
            if noms_sources and source.nom not in noms_sources:
                continue
            if not source.chemin.is_dir():
                log.error("Source inaccessible, ignorée : %s (%s)", source.nom, source.chemin)
                bilan["sources_inaccessibles"] += 1
                continue
            bilan += self._indexer_source(source, forcer)
        log.info("Vocabulaire : %d mots", rafraichir_vocabulaire(self.conn))
        return bilan

    def _indexer_source(self, source: Source, forcer: bool) -> Counter:
        bilan: Counter = Counter()
        source_id = self._enregistrer_source(source)
        debut = datetime.now(timezone.utc)
        log.info("Indexation de %s (%s)", source.nom, source.chemin)

        for i, chemin in enumerate(parcourir(source.chemin, self.cfg.exclure), start=1):
            try:
                bilan[self._indexer_fichier(source, source_id, chemin, forcer)] += 1
            except OSError as exc:
                log.warning("Illisible : %s (%s)", chemin, exc)
                bilan["illisible"] += 1
            if i % COMMIT_TOUS_LES == 0:
                self.conn.commit()
                log.info("  %d fichiers parcourus", i)

        # Les fichiers non revus pendant ce passage ont disparu de la source (ou sont désormais ignorés)
        supprimes = self.conn.execute(
            "UPDATE document SET supprime_le = now() "
            "WHERE source_id = %s AND vu_le < %s AND supprime_le IS NULL",
            (source_id, debut),
        ).rowcount
        self.conn.commit()
        bilan["supprime"] += supprimes
        return bilan

    def _enregistrer_source(self, source: Source) -> int:
        ligne = self.conn.execute(
            "INSERT INTO source (nom, chemin, pole) VALUES (%s, %s, %s) "
            "ON CONFLICT (nom) DO UPDATE SET chemin = EXCLUDED.chemin, pole = EXCLUDED.pole "
            "RETURNING id",
            (source.nom, str(source.chemin), source.pole),
        ).fetchone()
        self.conn.commit()
        return ligne["id"]

    def _indexer_fichier(self, source: Source, source_id: int, chemin: Path, forcer: bool) -> str:
        stat = chemin.stat()
        modifie_le = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)

        existant = self.conn.execute(
            "SELECT id, taille, modifie_le, empreinte, statut, methode, contenu, code_doc, nb_pages "
            "FROM document WHERE chemin = %s",
            (str(chemin),),
        ).fetchone()
        if existant and not forcer and existant["taille"] == stat.st_size and existant["modifie_le"] == modifie_le:
            self.conn.execute(
                "UPDATE document SET vu_le = now(), supprime_le = NULL WHERE id = %s", (existant["id"],)
            )
            return "inchange"

        signature = empreinte(chemin)
        if stat.st_size > self.cfg.taille_max_mo * 1024 * 1024:
            resultat = Resultat(statut="non_supporte", erreur=f"Fichier de plus de {self.cfg.taille_max_mo} Mo")
        else:
            resultat = extraire(chemin, self.cfg.soffice)

        # Contenu identique déjà passé en OCR : on garde le résultat (l'OCR est lent)
        if (existant and existant["methode"] == "ocr" and existant["empreinte"] == signature
                and resultat.statut == "ocr_requis"):
            resultat.statut = existant["statut"]
            resultat.contenu = existant["contenu"] or ""
            resultat.methode = "ocr"
            resultat.code_doc = existant["code_doc"]
            resultat.nb_pages = existant["nb_pages"]
            resultat.erreur = MESSAGE_SANS_TEXTE if existant["statut"] == "sans_texte" else None

        ligne = self.conn.execute(
            """
            INSERT INTO document (source_id, chemin, chemin_relatif, nom, extension, famille, taille,
                                  modifie_le, empreinte, statut, erreur, methode, titre, auteur,
                                  code_doc, nb_pages, contenu, vu_le, indexe_le, supprime_le)
            VALUES (%(source_id)s, %(chemin)s, %(chemin_relatif)s, %(nom)s, %(extension)s, %(famille)s,
                    %(taille)s, %(modifie_le)s, %(empreinte)s, %(statut)s, %(erreur)s, %(methode)s,
                    %(titre)s, %(auteur)s, %(code_doc)s, %(nb_pages)s, %(contenu)s, now(), now(), NULL)
            ON CONFLICT (chemin) DO UPDATE SET
                source_id = EXCLUDED.source_id, chemin_relatif = EXCLUDED.chemin_relatif,
                nom = EXCLUDED.nom, extension = EXCLUDED.extension, famille = EXCLUDED.famille,
                taille = EXCLUDED.taille, modifie_le = EXCLUDED.modifie_le, empreinte = EXCLUDED.empreinte,
                statut = EXCLUDED.statut, erreur = EXCLUDED.erreur, methode = EXCLUDED.methode,
                titre = EXCLUDED.titre, auteur = EXCLUDED.auteur, code_doc = EXCLUDED.code_doc,
                nb_pages = EXCLUDED.nb_pages, contenu = EXCLUDED.contenu,
                vu_le = now(), indexe_le = now(), supprime_le = NULL
            RETURNING id
            """,
            {
                "source_id": source_id,
                "chemin": str(chemin),
                "chemin_relatif": str(chemin.relative_to(source.chemin)),
                "nom": chemin.name,
                "extension": chemin.suffix.lower(),
                "famille": famille(chemin.suffix),
                "taille": stat.st_size,
                "modifie_le": modifie_le,
                "empreinte": signature,
                "statut": resultat.statut,
                "erreur": resultat.erreur,
                "methode": resultat.methode,
                "titre": resultat.titre,
                "auteur": resultat.auteur,
                "code_doc": resultat.code_doc,
                "nb_pages": resultat.nb_pages,
                # PostgreSQL refuse le caractère NUL dans un texte
                "contenu": resultat.contenu.replace("\x00", ""),
            },
        ).fetchone()

        self._appliquer_tags(ligne["id"], f"{resultat.titre or ''} {chemin.relative_to(source.chemin)}", resultat.contenu)
        return resultat.statut

    # --- OCR --------------------------------------------------------------------------

    def _vocabulaire(self) -> set[str]:
        return {r["mot"] for r in self.conn.execute("SELECT mot FROM vocabulaire WHERE length(mot) >= 4")}

    def ocr(self, limite: int | None = None) -> Counter:
        if not self.cfg.tessdata:
            raise SystemExit("OCR : renseignez [ocr] tessdata dans config.toml.")
        bilan: Counter = Counter()
        vocabulaire = self._vocabulaire()
        a_traiter = self.conn.execute(
            "SELECT d.id, d.chemin, d.titre, d.chemin_relatif FROM document d "
            "WHERE d.statut = 'ocr_requis' AND d.supprime_le IS NULL ORDER BY d.modifie_le DESC LIMIT %s",
            (limite,),
        ).fetchall()
        log.info("OCR : %d documents à traiter", len(a_traiter))

        for i, doc in enumerate(a_traiter, start=1):
            log.info("  [%d/%d] %s", i, len(a_traiter), doc["chemin_relatif"])
            resultat = ocr(Path(doc["chemin"]), self.cfg.tessdata, self.cfg.langue_ocr, self.cfg.dpi_ocr)
            if resultat.statut == "indexe" and not texte_exploitable(resultat.contenu, vocabulaire):
                resultat = Resultat(statut="sans_texte", methode="ocr", nb_pages=resultat.nb_pages,
                                    erreur=MESSAGE_SANS_TEXTE)

            if resultat.statut == "erreur":
                self.conn.execute(
                    "UPDATE document SET statut = 'erreur', erreur = %s WHERE id = %s", (resultat.erreur, doc["id"])
                )
            else:
                # Un PDF mixte peut déjà contenir un peu de texte : l'OCR le remplace (il le contient)
                self.conn.execute(
                    "UPDATE document SET contenu = %s, statut = %s, methode = 'ocr', erreur = %s, "
                    "code_doc = coalesce(code_doc, %s), nb_pages = coalesce(nb_pages, %s), indexe_le = now() "
                    "WHERE id = %s",
                    (resultat.contenu.replace("\x00", ""), resultat.statut, resultat.erreur,
                     resultat.code_doc, resultat.nb_pages, doc["id"]),
                )
                self._appliquer_tags(doc["id"], f"{doc['titre'] or ''} {doc['chemin_relatif']}", resultat.contenu)
            self.conn.commit()
            bilan[resultat.statut] += 1

        if a_traiter:
            log.info("Vocabulaire : %d mots", rafraichir_vocabulaire(self.conn))
        return bilan

    # --- Recalcul sans relire les fichiers ----------------------------------------------

    def recalculer(self) -> Counter:
        """Réapplique taxonomie, détection de code et filtre OCR au contenu déjà en base.

        À lancer après une modification de taxonomie.toml ou des règles : aucun fichier n'est relu.
        """
        bilan: Counter = Counter()
        rafraichir_vocabulaire(self.conn)
        vocabulaire = self._vocabulaire()
        documents = self.conn.execute(
            "SELECT id, titre, chemin_relatif, statut, methode, contenu FROM document WHERE supprime_le IS NULL"
        ).fetchall()
        for doc in documents:
            contenu = doc["contenu"] or ""
            if doc["methode"] == "ocr" and doc["statut"] == "indexe" and not texte_exploitable(contenu, vocabulaire):
                self.conn.execute(
                    "UPDATE document SET statut = 'sans_texte', contenu = '', erreur = %s, code_doc = NULL "
                    "WHERE id = %s",
                    (MESSAGE_SANS_TEXTE, doc["id"]),
                )
                contenu = ""
                bilan["ocr_ecarte"] += 1
            else:
                self.conn.execute(
                    "UPDATE document SET code_doc = %s WHERE id = %s", (detecter_code(contenu[:5000]), doc["id"])
                )
            self._appliquer_tags(doc["id"], f"{doc['titre'] or ''} {doc['chemin_relatif']}", contenu)
            bilan["recalcule"] += 1
        self.conn.commit()
        bilan["vocabulaire"] = rafraichir_vocabulaire(self.conn)
        return bilan

    # --- Tags ---------------------------------------------------------------------------

    def _appliquer_tags(self, document_id: int, nom: str, contenu: str) -> None:
        """Remplace les tags automatiques ; les tags posés à la main sont conservés."""
        self.conn.execute("DELETE FROM document_tag WHERE document_id = %s AND origine = 'auto'", (document_id,))
        for tag in self.taxonomie.proposer(nom, contenu):
            self.conn.execute(
                "INSERT INTO document_tag (document_id, tag_id, origine) VALUES (%s, %s, 'auto') "
                "ON CONFLICT DO NOTHING",
                (document_id, self._id_tag(tag.categorie, tag.libelle)),
            )

    def _id_tag(self, categorie: str, libelle: str) -> int:
        cle = (categorie, libelle)
        if cle not in self._ids_tags:
            self._ids_tags[cle] = self.conn.execute(
                "INSERT INTO tag (categorie, libelle) VALUES (%s, %s) "
                "ON CONFLICT (categorie, libelle) DO UPDATE SET libelle = EXCLUDED.libelle RETURNING id",
                cle,
            ).fetchone()["id"]
        return self._ids_tags[cle]
