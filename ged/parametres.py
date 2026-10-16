"""Réglages de la GED en base (table parametre) : lecture, écriture, formulaire.

Un réglage par clé, valeur en JSON. Le même formulaire sert à l'assistant d'installation et à la
page Réglages des administrateurs (gabarit _reglages.html) ; l'adresse d'écoute du serveur, lue
avant la base, est dans connexion.toml (voir ged.config).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from ged.config import Config, Connexion, Source

PROTOCOLES = ("NTLM", "Negotiate")

DEFAUTS: dict[str, object] = {
    # [{"nom", "chemin", "pole", "depot"}] : depot = la GED peut y ranger les documents déposés
    "sources": [],
    "exclure_manuels": [],
    "exclure_prepares": [],
    "taille_max_mo": 100,
    "ocr_active": False,
    "tessdata": "",
    "langue_ocr": "fra",
    "dpi_ocr": 300,
    "soffice": "",
    "exemples_recherche": "",
    "authentification": True,
    "protocole": "NTLM",
    "groupes_admin": [],
    "duree_session_heures": 8,
    "attente_depot": "depot",
    "taille_max_depot_mo": 50,
}
# Réglages lus une fois au démarrage du serveur : changés, ils attendent le prochain démarrage
AU_DEMARRAGE = {
    "authentification": "identification", "protocole": "protocole d'identification",
    "groupes_admin": "administrateurs", "duree_session_heures": "durée des sessions",
}
INSTALLEE = "installee_le"


# --- Lecture, écriture -----------------------------------------------------------------------------


def lire(conn: psycopg.Connection) -> dict:
    """Tous les réglages (valeur par défaut pour ceux jamais enregistrés)."""
    valeurs = {cle: valeur for cle, valeur in DEFAUTS.items()}
    for ligne in conn.execute("SELECT cle, valeur FROM parametre WHERE cle = ANY(%s)", (list(DEFAUTS),)):
        valeurs[ligne["cle"]] = ligne["valeur"]
    return valeurs


def est_installee(conn: psycopg.Connection) -> bool:
    return conn.execute("SELECT 1 FROM parametre WHERE cle = %s", (INSTALLEE,)).fetchone() is not None


def ecrire(conn: psycopg.Connection, valeurs: dict, par: str, installation: bool = False) -> list[str]:
    """Enregistre ces réglages. Renvoie les clés modifiées. `installation` : fin de l'assistant."""
    avant = lire(conn)
    modifies = [cle for cle, valeur in valeurs.items() if cle in DEFAUTS and avant.get(cle) != valeur]
    for cle in modifies:
        conn.execute(
            "INSERT INTO parametre (cle, valeur, modifie_par, modifie_le) VALUES (%s, %s, %s, now()) "
            "ON CONFLICT (cle) DO UPDATE SET valeur = EXCLUDED.valeur, modifie_par = EXCLUDED.modifie_par, "
            "modifie_le = now()",
            (cle, Jsonb(valeurs[cle]), par),
        )
    if installation:
        conn.execute(
            "INSERT INTO parametre (cle, valeur, modifie_par) VALUES (%s, to_jsonb(now()::text), %s) "
            "ON CONFLICT (cle) DO UPDATE SET valeur = EXCLUDED.valeur, modifie_par = EXCLUDED.modifie_par, "
            "modifie_le = now()",
            (INSTALLEE, par),
        )
    conn.commit()
    return modifies


def chemin_local(valeur: str) -> Path:
    """Chemin d'un dossier de la GED : relatif, il part du dossier où elle est lancée."""
    chemin = Path(valeur)
    return chemin if chemin.is_absolute() else Path.cwd() / chemin


def config(connexion: Connexion, valeurs: dict) -> Config:
    """Configuration complète à partir de connexion.toml et des réglages en base."""
    sources = valeurs["sources"]
    return Config(
        url_base=connexion.url,
        hote=connexion.hote,
        port=connexion.port,
        sources=[Source(nom=s["nom"], chemin=Path(s["chemin"]), pole=s.get("pole") or s["nom"]) for s in sources],
        taille_max_mo=int(valeurs["taille_max_mo"]),
        exclure=list(valeurs["exclure_manuels"]) + list(valeurs["exclure_prepares"]),
        exclure_manuels=list(valeurs["exclure_manuels"]),
        exclure_prepares=list(valeurs["exclure_prepares"]),
        ocr_active=bool(valeurs["ocr_active"]),
        tessdata=valeurs["tessdata"] or None,
        langue_ocr=valeurs["langue_ocr"],
        dpi_ocr=int(valeurs["dpi_ocr"]),
        soffice=valeurs["soffice"] or None,
        exemples_recherche=valeurs["exemples_recherche"],
        authentification=bool(valeurs["authentification"]),
        protocole=valeurs["protocole"],
        groupes_admin=list(valeurs["groupes_admin"]),
        duree_session_heures=float(valeurs["duree_session_heures"]),
        sources_depot=[s["nom"] for s in sources if s.get("depot")],
        attente_depot=chemin_local(valeurs["attente_depot"]),
        taille_max_depot_mo=int(valeurs["taille_max_depot_mo"]),
    )


# --- Valeurs proposées à l'installation ----------------------------------------------------------


def _premier_existant(*chemins: Path) -> str:
    return next((str(c) for c in chemins if c.exists()), "")


def detections() -> dict:
    """Réglages proposés par l'assistant pour une nouvelle installation, d'après ce poste."""
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    programmes = Path(os.environ.get("PROGRAMFILES",r"C:\Program Files"))
    tessdata = next((str(d) for d in (local / "ged" / "tessdata", programmes / "Tesseract-OCR" / "tessdata")
                     if (d / "fra.traineddata").is_file()), "")
    compte = "\\".join(filter(None, (os.environ.get("USERDOMAIN"), os.environ.get("USERNAME"))))
    return {
        **DEFAUTS,
        "tessdata": tessdata,
        "ocr_active": bool(tessdata),
        "soffice": _premier_existant(programmes / "LibreOffice" / "program" / "soffice.exe",
                                     local / "Programs" / "LibreOffice" / "program" / "soffice.exe"),
        "groupes_admin": [compte] if compte else [],
        "attente_depot": str(Path.cwd() / "depot"),
        "exemples_recherche": "imprimante, identitovigilance, \"message d'erreur\"",
    }


# --- Formulaire (assistant et page Réglages) ------------------------------------------------------


@dataclass
class Formulaire:
    valeurs: dict
    hote: str
    port: int | str
    erreurs: list[str] = field(default_factory=list)
    avertissements: list[str] = field(default_factory=list)


def _lignes(texte: str) -> list[str]:
    return [ligne.strip() for ligne in texte.splitlines() if ligne.strip()]


def lire_formulaire(champs: dict[str, list[str]], actuelles: dict) -> Formulaire:
    """Réglages saisis dans le formulaire, vérifiés. `actuelles` : réglages en base (ceux que le
    formulaire ne montre pas, comme les exclusions de « ged preparer », sont gardés)."""
    def un(cle: str, defaut: str = "") -> str:
        return (champs.get(cle) or [defaut])[0].strip()

    erreurs: list[str] = []
    avertissements: list[str] = []

    def entier(cle: str, libelle: str, minimum: int = 1, maximum: int = 10**6) -> int | str:
        texte = un(cle)
        if texte.isdigit() and minimum <= int(texte) <= maximum:
            return int(texte)
        erreurs.append(f"{libelle} : un nombre entier entre {minimum} et {maximum}.")
        return texte

    # Sources : une ligne par source, les lignes sans nom ni chemin sont ignorées
    noms, chemins, poles = (champs.get(f"source_{c}", []) for c in ("nom", "chemin", "pole"))
    depots = set(champs.get("source_depot", []))
    sources = []
    for i, (nom, chemin, pole) in enumerate(zip(noms, chemins, poles, strict=False)):
        nom, chemin, pole = nom.strip(), chemin.strip(), pole.strip()
        if not nom and not chemin:
            continue
        if not nom or not chemin:
            erreurs.append(f"Source « {nom or chemin} » : nom et chemin sont obligatoires.")
        elif any(s["nom"] == nom for s in sources):
            erreurs.append(f"Deux sources s'appellent « {nom} ».")
        elif not Path(chemin).is_dir():
            avertissements.append(f"Source « {nom} » : dossier inaccessible pour l'instant ({chemin}).")
        sources.append({"nom": nom, "chemin": chemin, "pole": pole or nom, "depot": str(i) in depots})

    authentification = un("authentification") == "1"
    groupes = _lignes(un("groupes_admin"))
    if authentification and not groupes:
        erreurs.append("Identification Windows : indiquez au moins un compte ou groupe administrateur, "
                       "sinon personne ne pourra plus régler la GED.")
    protocole = un("protocole", "NTLM")
    if protocole not in PROTOCOLES:
        erreurs.append("Protocole d'identification : NTLM ou Negotiate.")
    try:
        duree = float(un("duree_session_heures").replace(",", "."))
        if not 0.25 <= duree <= 24 * 30:
            raise ValueError
    except ValueError:
        erreurs.append("Durée des sessions : un nombre d'heures (8 par exemple).")
        duree = un("duree_session_heures")

    ocr_active = un("ocr_active") == "1"
    tessdata = un("tessdata")
    langue = un("langue_ocr", "fra") or "fra"
    if ocr_active and not tessdata:
        erreurs.append("OCR : indiquez le dossier du fichier de langue (tessdata), ou désactivez l'OCR.")
    elif ocr_active and not (Path(tessdata) / f"{langue}.traineddata").is_file():
        avertissements.append(f"OCR : {langue}.traineddata introuvable dans {tessdata}.")
    soffice = un("soffice")
    if soffice and not Path(soffice).is_file():
        avertissements.append(f"LibreOffice introuvable : {soffice} (les anciens .doc, .xls, .ppt ne seront pas lus).")

    attente = un("attente_depot")
    if any(s["depot"] for s in sources) and not attente:
        erreurs.append("Dépôt : indiquez le dossier d'attente des fichiers déposés.")

    hote = {"local": "127.0.0.1", "reseau": "0.0.0.0"}.get(un("ecoute"), un("hote_autre"))
    if not hote:
        erreurs.append("Serveur : indiquez l'adresse d'écoute.")
    port = entier("port", "Port du serveur", 1, 65535)

    valeurs = {
        **actuelles,
        "sources": sources,
        "exclure_manuels": _lignes(un("exclure_manuels")),
        "taille_max_mo": entier("taille_max_mo", "Taille maximale lue (Mo)", 1, 10000),
        "ocr_active": ocr_active,
        "tessdata": tessdata,
        "langue_ocr": langue,
        "dpi_ocr": entier("dpi_ocr", "Résolution de l'OCR (dpi)", 72, 1200),
        "soffice": soffice,
        "exemples_recherche": un("exemples_recherche"),
        "authentification": authentification,
        "protocole": protocole,
        "groupes_admin": groupes,
        "duree_session_heures": duree,
        "attente_depot": attente or DEFAUTS["attente_depot"],
        "taille_max_depot_mo": entier("taille_max_depot_mo", "Taille maximale d'un dépôt (Mo)", 1, 10000),
    }
    return Formulaire(valeurs, hote, port, erreurs, avertissements)
