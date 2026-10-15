"""Taxonomie en base : mots-clés des tags (colonne tag.mots_cles) et réglages (table reglage).

Elle se modifie depuis la page Tags (avec aperçu avant d'enregistrer). taxonomie.toml sert
d'import (d'office au premier démarrage, puis « ged importer-taxonomie ») et d'export
(« ged exporter-taxonomie », bouton de la page) : sauvegarde, versionnage, travail hors ligne.

Après une modification, les tags se recalculent sans relire les fichiers : un seul tag quand
ses mots-clés changent (recalculer_tag), tous quand les réglages changent (recalculer_tous).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg

from ged.tags_manuels import TagInvalide, auteur, homonyme, id_tag, journaliser, reappliquer
from ged.taxonomie import (
    DENSITE_DEFAUT, PREFIXE_TITRE, SEUIL_DEFAUT, Definition, Tag, Taxonomie, en_toml, lire_toml, regle,
    sans_prefixe,
)
from ged.texte import normaliser

if TYPE_CHECKING:
    from ged.acces import Lecteur

log = logging.getLogger(__name__)

MOT_MAX = 100
MOTS_MAX = 200
EXEMPLES = 10
PAQUET = 200
# Le vecteur de recherche n'indexe que le début du texte (voir schema.sql) : au-delà, le
# pré-filtre plein texte pourrait manquer un document, on le relit donc toujours
CONTENU_INDEXE = 500000

Avancement = Callable[[int, int], None]


# --- Lecture --------------------------------------------------------------------------------


def reglages(conn: psycopg.Connection) -> tuple[int, float]:
    """(seuil, densite_min)."""
    valeurs = {ligne["cle"]: ligne["valeur"] for ligne in conn.execute(
        "SELECT cle, valeur FROM reglage WHERE cle IN ('seuil', 'densite_min')"
    )}
    return int(valeurs.get("seuil", SEUIL_DEFAUT)), float(valeurs.get("densite_min", DENSITE_DEFAUT))


def definition(conn: psycopg.Connection) -> Definition:
    """Tous les tags et leurs mots-clés (liste vide pour un tag sans), avec les réglages."""
    seuil, densite_min = reglages(conn)
    mots = {
        Tag(ligne["categorie"], ligne["libelle"]): list(ligne["mots_cles"] or [])
        for ligne in conn.execute("SELECT categorie, libelle, mots_cles FROM tag ORDER BY categorie, libelle")
    }
    return Definition(mots=mots, seuil=seuil, densite_min=densite_min)


def charger(conn: psycopg.Connection) -> Taxonomie:
    """La taxonomie appliquée par l'indexation et les recalculs."""
    return Taxonomie.construire(definition(conn))


def exporter(conn: psycopg.Connection) -> str:
    """Texte de taxonomie.toml, relisible par « ged importer-taxonomie »."""
    return en_toml(definition(conn))


# --- Modification ---------------------------------------------------------------------------


def nettoyer_mots(mots: list[str]) -> list[str]:
    """Mots-clés en minuscules, espaces réduits, préfixe « titre: » unifié, sans doublon."""
    propres: list[str] = []
    for mot in mots:
        mot = " ".join(mot.split()).lower()
        if titre := re.match(r"titre\s*:\s*", mot):
            mot = PREFIXE_TITRE + mot[titre.end():]
        if not sans_prefixe(mot).strip():
            continue
        if len(mot) > MOT_MAX:
            raise TagInvalide(f"Mot-clé trop long ({MOT_MAX} caractères au plus) : « {mot[:30]}… ».")
        if not any(normaliser(mot) == normaliser(p) for p in propres):
            propres.append(mot)
    if len(propres) > MOTS_MAX:
        raise TagInvalide(f"{MOTS_MAX} mots-clés au plus par tag.")
    return propres


def _ecart(avant: list[str], apres: list[str]) -> str:
    """« +ajouté, −retiré » pour le journal."""
    ajoutes = [f"+{m}" for m in apres if m not in avant]
    retires = [f"−{m}" for m in avant if m not in apres]
    return ", ".join(ajoutes + retires)


def modifier_mots(conn: psycopg.Connection, tag_id: int, mots: list[str], lecteur: Lecteur | None,
                  par: str | None = None) -> bool:
    """Remplace les mots-clés du tag. Renvoie False s'ils n'ont pas changé."""
    ligne = conn.execute("SELECT mots_cles FROM tag WHERE id = %s", (tag_id,)).fetchone()
    if not ligne:
        raise TagInvalide("Ce tag n'existe plus.")
    avant, apres = list(ligne["mots_cles"] or []), nettoyer_mots(mots)
    if avant == apres:
        return False
    conn.execute("UPDATE tag SET mots_cles = %s WHERE id = %s", (apres or None, tag_id))
    journaliser(conn, par or auteur(lecteur), "mots-clés", tag_id, detail=_ecart(avant, apres) or "ordre modifié")
    return True


def modifier_reglages(conn: psycopg.Connection, seuil: int, densite_min: float, lecteur: Lecteur | None,
                      par: str | None = None) -> bool:
    """Change seuil et densité minimale. Renvoie False s'ils n'ont pas changé."""
    if not 1 <= seuil <= 100 or not 0 <= densite_min <= 100:
        raise TagInvalide("Seuil entre 1 et 100, densité entre 0 et 100.")
    avant = reglages(conn)
    if avant == (seuil, densite_min):
        return False
    for cle, valeur in (("seuil", str(seuil)), ("densite_min", str(densite_min))):
        conn.execute("INSERT INTO reglage (cle, valeur) VALUES (%s, %s) "
                     "ON CONFLICT (cle) DO UPDATE SET valeur = EXCLUDED.valeur", (cle, valeur))
    journaliser(conn, par or auteur(lecteur), "réglages",
                detail=f"seuil {avant[0]} → {seuil}, densité {avant[1]} → {densite_min}")
    return True


# --- Import ---------------------------------------------------------------------------------


def _tag_existant(conn: psycopg.Connection, tag: Tag) -> dict | None:
    return conn.execute(
        "SELECT id, mots_cles FROM tag WHERE categorie = %s AND libelle = %s", (tag.categorie, tag.libelle)
    ).fetchone() or homonyme(conn, tag.categorie, tag.libelle)


def differences(conn: psycopg.Connection, nouvelle: Definition) -> list[str]:
    """Ce que l'import de `nouvelle` changerait, une ligne par tag (pour confirmation)."""
    actuelle = definition(conn)
    lignes = []
    if (actuelle.seuil, actuelle.densite_min) != (nouvelle.seuil, nouvelle.densite_min):
        lignes.append(f"réglages : seuil {actuelle.seuil} → {nouvelle.seuil}, "
                      f"densité {actuelle.densite_min} → {nouvelle.densite_min}")
    retrouves = set()
    for tag, mots in nouvelle.mots.items():
        mots = nettoyer_mots(mots)
        existant = _tag_existant(conn, tag)
        if not existant:
            lignes.append(f"+ {tag.libelle} ({tag.categorie}) : nouveau tag, {', '.join(mots) or 'sans mot-clé'}")
            continue
        retrouves.add(existant["id"])
        avant = list(existant["mots_cles"] or [])
        if avant != mots:
            lignes.append(f"~ {tag.libelle} ({tag.categorie}) : {_ecart(avant, mots) or 'ordre modifié'}")
    for ligne in conn.execute("SELECT id, categorie, libelle, mots_cles FROM tag WHERE cardinality(mots_cles) > 0"):
        if ligne["id"] not in retrouves:
            lignes.append(f"- {ligne['libelle']} ({ligne['categorie']}) : absent du fichier, perd ses mots-clés "
                          f"({', '.join(ligne['mots_cles'])})")
    return lignes


def importer(conn: psycopg.Connection, nouvelle: Definition, par: str) -> int:
    """Le fichier devient la référence : ses tags sont créés au besoin et prennent ses mots-clés ;
    les autres tags perdent les leurs mais restent (avec leurs poses à la main et leurs dossiers).
    Renvoie le nombre de tags modifiés. Sans commit."""
    modifies = 0
    retrouves = set()
    for tag, mots in nouvelle.mots.items():
        existant = _tag_existant(conn, tag)
        if existant:
            tag_id = existant["id"]
        else:
            tag_id = id_tag(conn, tag.categorie, tag.libelle)
            conn.execute("UPDATE tag SET cree_le = now() WHERE id = %s", (tag_id,))
            journaliser(conn, par, "création", tag_id, detail="import de la taxonomie")
        retrouves.add(tag_id)
        modifies += modifier_mots(conn, tag_id, mots, None, par)
    for ligne in conn.execute("SELECT id FROM tag WHERE cardinality(mots_cles) > 0").fetchall():
        if ligne["id"] not in retrouves:
            modifies += modifier_mots(conn, ligne["id"], [], None, par)
    modifier_reglages(conn, nouvelle.seuil, nouvelle.densite_min, None, par)
    conn.execute("INSERT INTO reglage (cle, valeur) VALUES ('taxonomie_importee_le', %s) "
                 "ON CONFLICT (cle) DO UPDATE SET valeur = EXCLUDED.valeur", (datetime.now(UTC).isoformat(),))
    return modifies


def importer_au_premier_demarrage(conn: psycopg.Connection, fichier: Path) -> bool:
    """Importe taxonomie.toml si la base n'a jamais reçu de taxonomie. Avec commit.

    Une seule fois : si quelqu'un vide ensuite les mots-clés depuis la page, le fichier ne revient pas."""
    if conn.execute("SELECT 1 FROM reglage WHERE cle = 'taxonomie_importee_le'").fetchone():
        return False
    nouvelle = lire_toml(fichier.read_text(encoding="utf-8")) if fichier.is_file() else Definition()
    importer(conn, nouvelle, "import initial")
    conn.commit()
    log.info("Taxonomie importée de %s : %d tags avec mots-clés", fichier, sum(1 for m in nouvelle.mots.values() if m))
    return True


# --- Aperçu et recalcul d'un tag ------------------------------------------------------------


def _requete_plein_texte(conn: psycopg.Connection, mots: list[str]) -> str | None:
    """Requête plein texte qui trouve au moins tous les documents où un des mots-clés apparaît
    (racinisation : elle en trouve aussi d'autres, que la règle exacte écarte ensuite).
    None si un mot-clé n'y est pas exprimable (mots vides seulement) : il faut tout parcourir."""
    requetes = [ligne["q"] for ligne in conn.execute(
        "SELECT phraseto_tsquery('ged_fr', m)::text AS q FROM unnest(%s::text[]) m",
        ([sans_prefixe(m) for m in mots],),
    )]
    if not requetes or any(not q for q in requetes):
        return None
    return " | ".join(f"({q})" for q in requetes)


def candidats(conn: psycopg.Connection, tag_id: int | None, mots: list[str]) -> tuple[list[int], bool]:
    """Documents à examiner pour ce tag : ceux que le pré-filtre plein texte retient, et ceux qui
    portent déjà le tag par la taxonomie (pour savoir lesquels le perdraient).
    Renvoie (identifiants, parcours complet)."""
    requete = _requete_plein_texte(conn, mots) if mots else ""
    condition = "true" if requete is None else "false" if not requete else (
        f"d.recherche @@ %(requete)s::tsquery OR octet_length(d.contenu) > {CONTENU_INDEXE}"
    )
    ids = [ligne["id"] for ligne in conn.execute(
        f"SELECT d.id FROM document d WHERE d.supprime_le IS NULL AND ({condition} OR EXISTS ("
        "  SELECT 1 FROM document_tag dt WHERE dt.document_id = d.id AND dt.tag_id = %(tag)s AND dt.origine = 'auto'"
        ")) ORDER BY d.id",
        {"requete": requete, "tag": tag_id},
    )]
    return ids, requete is None


def _documents(conn: psycopg.Connection, ids: list[int]) -> Iterator[list[dict]]:
    """Documents par paquets, avec leur texte et l'état actuel du tag."""
    for debut in range(0, len(ids), PAQUET):
        yield conn.execute(
            "SELECT id, titre, chemin_relatif, contenu FROM document WHERE id = ANY(%s) ORDER BY id",
            (ids[debut:debut + PAQUET],),
        ).fetchall()


def _etat_du_tag(conn: psycopg.Connection, tag_id: int | None) -> tuple[dict[int, str], set[int]]:
    """(origine du tag par document, documents d'où un administrateur l'a retiré)."""
    if tag_id is None:
        return {}, set()
    poses = {ligne["document_id"]: ligne["origine"] for ligne in conn.execute(
        "SELECT document_id, origine FROM document_tag WHERE tag_id = %s", (tag_id,))}
    retires = {ligne["document_id"] for ligne in conn.execute(
        "SELECT document_id FROM tag_retire WHERE tag_id = %s", (tag_id,))}
    return poses, retires


def _nom(doc: dict) -> str:
    """Ce que la taxonomie lit comme nom (comme l'indexation) : titre et chemin."""
    return f"{doc['titre'] or ''} {doc['chemin_relatif']}"


def apercu(conn: psycopg.Connection, tag_id: int | None, mots: list[str]) -> dict:
    """Ce que donneraient ces mots-clés, sans rien modifier : documents qui porteraient le tag,
    gagnés et perdus par rapport à aujourd'hui (avec exemples), documents trouvés par chaque mot."""
    mots = nettoyer_mots(mots)
    seuil, densite_min = reglages(conn)
    tag = Tag("", "")
    taxonomie = Taxonomie({tag: r} if (r := regle(mots)) else {}, seuil, densite_min)
    par_mot = [(mot, Taxonomie({tag: r} if (r := regle([mot])) else {}, seuil, densite_min)) for mot in mots]
    poses, retires = _etat_du_tag(conn, tag_id)
    ids, complet = candidats(conn, tag_id, mots)
    resultat = {"total": 0, "actuels": sum(1 for o in poses.values() if o == "auto"), "nb_gagnes": 0,
                "gagnes": [], "nb_perdus": 0, "perdus": [], "nb_retires": 0, "parcours_complet": complet,
                "mots": [], "examines": len(ids)}
    nb_par_mot = dict.fromkeys(mots, 0)
    for paquet in _documents(conn, ids):
        for doc in paquet:
            nom, contenu = _nom(doc), doc["contenu"] or ""
            for mot, seul in par_mot:
                if seul.proposer(nom, contenu) and doc["id"] not in retires:
                    nb_par_mot[mot] += 1
            pose = bool(taxonomie.proposer(nom, contenu))
            origine = poses.get(doc["id"])
            if pose and doc["id"] in retires:
                resultat["nb_retires"] += 1
                pose = False
            resultat["total"] += pose
            exemple = {"id": doc["id"], "chemin": doc["chemin_relatif"]}
            if pose and origine is None:
                resultat["nb_gagnes"] += 1
                if len(resultat["gagnes"]) < EXEMPLES:
                    resultat["gagnes"].append({**exemple, "raison": taxonomie.expliquer(tag, nom, contenu)})
            elif not pose and origine == "auto":
                resultat["nb_perdus"] += 1
                if len(resultat["perdus"]) < EXEMPLES:
                    resultat["perdus"].append(exemple)
    for mot in mots:
        alerte = ""
        if not mot.startswith(PREFIXE_TITRE) and len(normaliser(mot)) <= 3:
            alerte = f"court : préférez « {PREFIXE_TITRE}{mot} » s'il n'a de sens que dans un nom de fichier"
        resultat["mots"].append({"mot": mot, "nb": nb_par_mot[mot], "alerte": alerte})
    return resultat


def recalculer_tag(conn: psycopg.Connection, tag_id: int, avancement: Avancement | None = None) -> dict:
    """Réapplique les mots-clés actuels d'un seul tag : pose ou retire ses poses « auto » (celles
    à la main ou par dossier ne bougent pas, un retrait par un administrateur est respecté).
    Commit par paquets. Renvoie {"examines", "poses", "retraits"}."""
    ligne = conn.execute("SELECT mots_cles FROM tag WHERE id = %s", (tag_id,)).fetchone()
    mots = list(ligne["mots_cles"] or []) if ligne else []
    seuil, densite_min = reglages(conn)
    tag = Tag("", "")
    taxonomie = Taxonomie({tag: r} if (r := regle(mots)) else {}, seuil, densite_min)
    poses, retires = _etat_du_tag(conn, tag_id)
    ids, _ = candidats(conn, tag_id, mots)
    bilan = {"examines": len(ids), "poses": 0, "retraits": 0}
    fait = 0
    for paquet in _documents(conn, ids):
        for doc in paquet:
            if taxonomie.proposer(_nom(doc), doc["contenu"] or "") and doc["id"] not in retires:
                if doc["id"] not in poses:
                    conn.execute("INSERT INTO document_tag (document_id, tag_id, origine) VALUES (%s, %s, 'auto') "
                                 "ON CONFLICT DO NOTHING", (doc["id"], tag_id))
                    bilan["poses"] += 1
            elif poses.get(doc["id"]) == "auto":
                conn.execute("DELETE FROM document_tag WHERE document_id = %s AND tag_id = %s AND origine = 'auto'",
                             (doc["id"], tag_id))
                bilan["retraits"] += 1
        conn.commit()
        fait += len(paquet)
        if avancement:
            avancement(fait, len(ids))
    return bilan


def recalculer_tous(conn: psycopg.Connection, avancement: Avancement | None = None) -> dict:
    """Réapplique toute la taxonomie et les tags de dossiers à tous les documents (comme la partie
    tags de « ged recalculer »). Commit par paquets. Renvoie {"examines"}."""
    taxonomie = charger(conn)
    ids_tags: dict[Tag, int] = {}
    ids = [ligne["id"] for ligne in conn.execute("SELECT id FROM document WHERE supprime_le IS NULL ORDER BY id")]
    fait = 0
    for paquet in _documents(conn, ids):
        for doc in paquet:
            proposes = []
            for tag in taxonomie.proposer(_nom(doc), doc["contenu"] or ""):
                if tag not in ids_tags:
                    ids_tags[tag] = id_tag(conn, tag.categorie, tag.libelle)
                proposes.append(ids_tags[tag])
            reappliquer(conn, doc["id"], proposes)
        conn.commit()
        fait += len(paquet)
        if avancement:
            avancement(fait, len(ids))
    return {"examines": len(ids)}
