"""Vérifications de l'installation (« ged verifier ») : ce qui fait échouer ou ralentit une mise en route.

Rien n'est modifié : la base est seulement interrogée sur sa version et la présence du schéma,
les sources sont seulement listées.
"""

from __future__ import annotations

import os
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from ged.config import Config

OK, ATTENTION, ERREUR = "ok", "attention", "erreur"


@dataclass
class Verification:
    sujet: str
    statut: str
    detail: str


def verifier_base(cfg: Config) -> Verification:
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    # Hôte, port et base seulement : jamais le mot de passe
    infos = conninfo_to_dict(cfg.url_base)
    cible = f"{infos.get('host', 'localhost')}:{infos.get('port', 5432)}/{infos.get('dbname', '?')}"
    try:
        with psycopg.connect(cfg.url_base, connect_timeout=5) as conn:
            version = conn.execute("SHOW server_version").fetchone()[0]
            schema = conn.execute("SELECT to_regclass('document') IS NOT NULL").fetchone()[0]
    except psycopg.Error as exc:
        message = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        return Verification("Base de données", ERREUR, f"{cible} injoignable : {message}")
    if not schema:
        return Verification("Base de données", ATTENTION, f"{cible} (PostgreSQL {version}) : schéma absent, lancez « ged init-db »")
    return Verification("Base de données", OK, f"{cible} (PostgreSQL {version})")


def verifier_sources(cfg: Config) -> list[Verification]:
    from ged.droits import lire_partage

    if not cfg.sources:
        return [Verification("Sources", ERREUR, "aucune section [[sources]] dans config.toml")]
    resultats = []
    for source in cfg.sources:
        sujet = f"Source « {source.nom} »"
        debut = time.monotonic()
        try:
            with os.scandir(source.chemin) as entrees:
                nb = sum(1 for _ in entrees)
        except OSError as exc:
            resultats.append(Verification(sujet, ERREUR, f"{source.chemin} illisible : {exc.strerror or exc}"))
            continue
        duree = time.monotonic() - debut
        detail = f"{source.chemin} : {nb} éléments à la racine"
        if duree > 2:
            resultats.append(Verification(sujet, ATTENTION, f"{detail}, lu en {duree:.0f} s (réseau lent ?)"))
        elif str(source.chemin).startswith("\\\\") and sys.platform == "win32" and lire_partage(source.chemin) is None:
            resultats.append(Verification(
                sujet, ATTENTION,
                f"{detail} ; droits du partage illisibles : seuls les droits NTFS filtreront les résultats",
            ))
        else:
            resultats.append(Verification(sujet, OK, detail))
    # Une source dans une autre : ses documents seraient indexés deux fois
    for a in cfg.sources:
        for b in cfg.sources:
            if a is not b and a.chemin != b.chemin and Path(b.chemin).is_relative_to(a.chemin):
                resultats.append(Verification(
                    "Sources", ATTENTION, f"« {b.nom} » est dans « {a.nom} » : ses documents seront indexés deux fois",
                ))
    return resultats


def verifier_exclusions(cfg: Config) -> Verification:
    nb_config, nb_total = len(cfg.exclure_config), len(cfg.exclure)
    motifs_config = f"{nb_config} motif{'s' if nb_config > 1 else ''} dans config.toml"
    if not cfg.fichier_exclusions.is_file():
        return Verification(
            "Exclusions", ATTENTION,
            f"{cfg.fichier_exclusions.name} absent : lancez « ged preparer » pour choisir les dossiers à indexer"
            + (f" ({motifs_config})" if nb_config else ""),
        )
    return Verification(
        "Exclusions", OK,
        f"{nb_total - nb_config} dossier(s) exclu(s) dans {cfg.fichier_exclusions.name}, {motifs_config}",
    )


def verifier_taxonomie(cfg: Config) -> Verification:
    """La taxonomie vit en base ; taxonomie.toml n'est importé qu'au premier démarrage."""
    import psycopg

    from ged.taxonomie import lire_toml

    try:
        with psycopg.connect(cfg.url_base, connect_timeout=5) as conn:
            if conn.execute("SELECT to_regclass('reglage') IS NOT NULL").fetchone()[0] and conn.execute(
                "SELECT 1 FROM reglage WHERE cle = 'taxonomie_importee_le'"
            ).fetchone():
                nb = conn.execute("SELECT count(*) FROM tag WHERE cardinality(mots_cles) > 0").fetchone()[0]
                if not nb:
                    return Verification("Taxonomie", ATTENTION, "aucun tag n'a de mot-clé : aucun tag ne sera posé")
                return Verification("Taxonomie", OK, f"{nb} tags avec des mots-clés (en base, page Tags)")
    except psycopg.Error:
        pass  # base injoignable : signalé par la vérification de la base
    if not cfg.taxonomie.is_file():
        return Verification("Taxonomie", ATTENTION, f"{cfg.taxonomie} absent : aucun tag ne sera posé")
    try:
        definition = lire_toml(cfg.taxonomie.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        return Verification("Taxonomie", ERREUR, f"{cfg.taxonomie.name} illisible : {exc}")
    nb = sum(1 for mots in definition.mots.values() if mots)
    return Verification("Taxonomie", OK, f"{cfg.taxonomie.name} : {nb} tags, importés au premier démarrage")


def verifier_outils(cfg: Config) -> list[Verification]:
    resultats = []
    if cfg.ocr_active:
        langue = Path(cfg.tessdata or "") / f"{cfg.langue_ocr}.traineddata"
        if cfg.tessdata and langue.is_file():
            resultats.append(Verification("OCR", OK, f"langue {cfg.langue_ocr} trouvée dans {cfg.tessdata}"))
        else:
            resultats.append(Verification("OCR", ERREUR, f"{langue} introuvable : les scans ne seront pas lus"))
    else:
        resultats.append(Verification("OCR", ATTENTION, "désactivé : les PDF scannés resteront sans texte"))
    if not cfg.soffice:
        resultats.append(Verification("LibreOffice", ATTENTION, "non configuré : .doc, .xls et .ppt anciens non lus"))
    elif Path(cfg.soffice).is_file():
        resultats.append(Verification("LibreOffice", OK, cfg.soffice))
    else:
        resultats.append(Verification("LibreOffice", ATTENTION, f"{cfg.soffice} introuvable : .doc, .xls et .ppt anciens non lus"))
    return resultats


def verifier_serveur(cfg: Config) -> Verification:
    portee = "ce poste seulement" if cfg.hote in ("127.0.0.1", "localhost") else "les autres postes"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1" if cfg.hote == "localhost" else cfg.hote, cfg.port))
        except OSError:
            return Verification(
                "Interface web", ATTENTION, f"port {cfg.port} déjà utilisé (la GED tourne déjà, ou un autre programme)"
            )
    return Verification("Interface web", OK, f"port {cfg.port} libre, accessible depuis {portee}")


def verifier_authentification(cfg: Config) -> list[Verification]:
    if not cfg.authentification:
        return [Verification("Identification", ATTENTION, "désactivée : chacun voit tous les documents")]
    if sys.platform != "win32":
        return [Verification("Identification", ERREUR, "l'identification Windows ne fonctionne que sous Windows")]
    try:
        import pywintypes
        import win32security
    except ImportError:
        return [Verification("Identification", ERREUR, "pywin32 absent : relancez l'installation (uv sync)")]
    resultats = [Verification("Identification", OK, f"session Windows, protocole {cfg.protocole}")]
    if not cfg.groupes_admin:
        resultats.append(Verification(
            "Administrateurs", ATTENTION, "aucun groupe dans groupes_admin : personne ne verra la page État de l'index",
        ))
    for nom in cfg.groupes_admin:
        if nom.upper().startswith("S-1-"):
            continue
        try:
            win32security.LookupAccountName(None, nom)
        except pywintypes.error as exc:
            resultats.append(Verification("Administrateurs", ERREUR, f"groupe {nom} introuvable ({exc.strerror})"))
        else:
            resultats.append(Verification("Administrateurs", OK, nom))
    return resultats


# Au-delà, la page « Mes groupes » et la fiche document (« vous y avez accès via... ») traînent
GROUPES_LENT_S = 2


def sids_du_compte_courant() -> tuple[str, set[str]]:
    """SID et groupes (imbrication comprise) du compte qui lance la GED, lus dans son jeton :
    les mêmes informations que celles d'une personne qui se connecte à l'interface."""
    import win32api
    import win32security

    from ged.annuaire import _PAS_DES_GROUPES

    jeton = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    try:
        utilisateur = win32security.GetTokenInformation(jeton, win32security.TokenUser)[0]
        groupes = win32security.GetTokenInformation(jeton, win32security.TokenGroups)
    finally:
        jeton.Close()
    sids = {win32security.ConvertSidToStringSid(sid) for sid, _ in groupes}
    return win32security.ConvertSidToStringSid(utilisateur), {s for s in sids if not s.startswith(_PAS_DES_GROUPES)}


def resume_groupes(compte: str, noms: dict[str, str], duree_noms: float, annuaire: dict | None, duree_annuaire: float) -> Verification:
    """`noms` : SID -> nom lu ; `annuaire` : graphe des groupes du domaine (None : pas de domaine)."""
    inconnus = [sid for sid, nom in noms.items() if nom == sid]
    detail = f"{compte} : {len(noms)} groupes, noms lus en {duree_noms:.1f} s"
    if annuaire is None:
        return Verification("Groupes", ATTENTION, f"{detail} ; pas d'annuaire Active Directory joignable (hors domaine ?) : "
                            "les groupes s'affichent sans leur hiérarchie")
    detail += f", hiérarchie de {len(annuaire)} groupes du domaine lue en {duree_annuaire:.1f} s"
    if inconnus:
        return Verification("Groupes", ATTENTION, f"{detail} ; {len(inconnus)} groupes sans nom (contrôleur de domaine lent ou injoignable)")
    if duree_noms + duree_annuaire > GROUPES_LENT_S:
        return Verification("Groupes", ATTENTION, f"{detail} : lent, les pages « Mes groupes » et document le seront aussi")
    return Verification("Groupes", OK, detail)


def verifier_groupes() -> list[Verification]:
    """Les groupes remontent-ils, et en combien de temps ? Mesuré sur le compte qui lance la GED."""
    if sys.platform != "win32":
        return []
    from ged.annuaire import AnnuaireAD
    from ged.droits import nom_compte

    utilisateur, groupes = sids_du_compte_courant()
    debut = time.perf_counter()
    noms = {sid: nom_compte(sid) for sid in groupes}
    duree_noms = time.perf_counter() - debut
    annuaire = AnnuaireAD()
    debut = time.perf_counter()
    graphe = annuaire.graphe(groupes | {utilisateur}) if annuaire.disponible else None
    duree_annuaire = time.perf_counter() - debut
    return [resume_groupes(nom_compte(utilisateur), noms, duree_noms, graphe, duree_annuaire)]


# Échantillon de fichiers dont on lit le propriétaire et les droits : quelques-uns par dossier,
# pour couvrir l'arborescence plutôt que le premier dossier venu, dans un temps borné
ECHANTILLON_FICHIERS = 200
ECHANTILLON_PAR_DOSSIER = 5
ECHANTILLON_DUREE_S = 15
# Au-delà, la lecture des droits allonge sensiblement l'indexation
DROITS_LENT_MS = 50


def resume_proprietaires(sujet: str, proprietaires: list[str | None], duree: float) -> Verification:
    """`proprietaires` : propriétaire de chaque fichier de l'échantillon (None : droits illisibles)."""
    from collections import Counter

    if not proprietaires:
        return Verification(sujet, ATTENTION, "aucun fichier à échantillonner")
    lus = Counter(p for p in proprietaires if p)
    illisibles = proprietaires.count(None)
    sans_nom = sum(nb for nom, nb in lus.items() if nom.upper().startswith("S-1-"))
    par_fichier = duree / len(proprietaires) * 1000
    principaux = ", ".join(f"{nom} ({nb})" for nom, nb in lus.most_common(3))
    detail = (f"{len(proprietaires)} fichiers échantillonnés : {len(lus)} propriétaire{'s' if len(lus) > 1 else ''}"
              + (f" dont {principaux}" if principaux else "") + f" ; droits lus en {par_fichier:.0f} ms/fichier")
    if illisibles:
        return Verification(sujet, ATTENTION, f"{detail} ; {illisibles} fichiers aux droits illisibles : "
                            "ils seront masqués à tous, sauf aux administrateurs de la GED")
    if sans_nom:
        return Verification(sujet, ATTENTION, f"{detail} ; {sans_nom} propriétaires sans nom (compte supprimé ou domaine injoignable)")
    if par_fichier > DROITS_LENT_MS:
        return Verification(sujet, ATTENTION, f"{detail} : lent, l'indexation le sera aussi")
    return Verification(sujet, OK, detail)


def verifier_proprietaires(cfg: Config) -> list[Verification]:
    """Propriétaires et droits d'un échantillon de fichiers de chaque source (sans les ouvrir)."""
    if sys.platform != "win32":
        return []
    from collections import Counter

    from ged.droits import lire_securite
    from ged.indexeur import parcourir

    resultats = []
    for source in cfg.sources:
        if not source.chemin.is_dir():
            continue
        proprietaires, par_dossier = [], Counter()
        debut, duree_droits = time.perf_counter(), 0.0
        for chemin in parcourir(source.chemin, cfg.exclure):
            if len(proprietaires) >= ECHANTILLON_FICHIERS or time.perf_counter() - debut > ECHANTILLON_DUREE_S:
                break
            if par_dossier[chemin.parent] >= ECHANTILLON_PAR_DOSSIER:
                continue
            par_dossier[chemin.parent] += 1
            # Seule la lecture des droits est chronométrée : c'est ce qu'elle ajoute à l'indexation
            debut_droits = time.perf_counter()
            securite = lire_securite(chemin)
            duree_droits += time.perf_counter() - debut_droits
            proprietaires.append(securite.proprietaire if securite.acces_sids is not None else None)
        resultats.append(resume_proprietaires(f"Propriétaires « {source.nom} »", proprietaires, duree_droits))
    return resultats


def verifier(cfg: Config) -> list[Verification]:
    return [
        verifier_base(cfg),
        *verifier_sources(cfg),
        verifier_exclusions(cfg),
        verifier_taxonomie(cfg),
        *verifier_outils(cfg),
        verifier_serveur(cfg),
        *verifier_authentification(cfg),
        *verifier_groupes(),
        *verifier_proprietaires(cfg),
    ]


SYMBOLES = {OK: "[ OK ]", ATTENTION: "[ !! ]", ERREUR: "[ERREUR]"}


def afficher(resultats: list[Verification]) -> bool:
    """Affiche la liste ; True si aucune erreur."""
    largeur = max(len(r.sujet) for r in resultats)
    for r in resultats:
        print(f"{SYMBOLES[r.statut]:<8} {r.sujet:<{largeur}}  {r.detail}")
    erreurs = sum(r.statut == ERREUR for r in resultats)
    alertes = sum(r.statut == ATTENTION for r in resultats)
    print()
    if erreurs:
        print(f"{erreurs} erreur(s) à corriger avant d'indexer, {alertes} point(s) d'attention.")
    elif alertes:
        print(f"Prêt à indexer, avec {alertes} point(s) d'attention.")
    else:
        print("Tout est prêt.")
    return not erreurs
