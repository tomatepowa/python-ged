"""Extraction du texte et des métadonnées des fichiers.

Tous les fichiers sont ouverts en lecture seule. Les formats Office récents (OOXML) et
OpenDocument sont lus directement (ce sont des zip de XML) ; les PDF via PyMuPDF ;
les anciens formats Office via une conversion LibreOffice optionnelle.
"""

from __future__ import annotations

import email
import email.policy
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from ged.texte import detecter_code

FAMILLES: dict[str, tuple[str, ...]] = {
    "Word": (".docx", ".docm", ".doc", ".dotx", ".dot", ".odt", ".rtf"),
    "PDF": (".pdf",),
    "Image": (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif", ".heic", ".webp"),
    "Excel": (".xlsx", ".xlsm", ".xls", ".ods", ".csv"),
    "PowerPoint": (".pptx", ".ppt", ".odp"),
    "Visio": (".vsdx", ".vsd"),
    "Mail": (".msg", ".eml"),
    "Texte": (".txt", ".md", ".log", ".xml", ".json", ".ini", ".html", ".htm", ".yaml", ".yml"),
    "Script": (".ps1", ".psm1", ".bat", ".cmd", ".vbs", ".sql", ".py", ".sh", ".reg"),
    "Archive": (".zip", ".7z", ".rar", ".gz", ".tar", ".cab"),
    "Video": (".mp4", ".mov", ".avi", ".mkv", ".wmv"),
}
_FAMILLE_PAR_EXTENSION = {ext: fam for fam, exts in FAMILLES.items() for ext in exts}

# Images que l'OCR sait lire
EXTENSIONS_OCR = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp")
# Anciens formats binaires : lisibles uniquement via LibreOffice
EXTENSIONS_CONVERSION = (".doc", ".dot", ".rtf", ".xls", ".ppt", ".vsd")
EXTENSIONS_TEXTE = FAMILLES["Texte"] + FAMILLES["Script"] + (".csv",)

TAILLE_MAX_TEXTE = 5 * 1024 * 1024
# En dessous de ce nombre de caractères par page, un PDF est considéré comme scanné
CARACTERES_MIN_PAR_PAGE = 30

NS_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
NS_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
NS_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
NS_DC = "{http://purl.org/dc/elements/1.1/}"
NS_CP = "{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}"
NS_TEXT_ODF = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"


@dataclass
class Resultat:
    # indexe | ocr_requis | non_supporte | erreur
    statut: str
    contenu: str = ""
    methode: str | None = None
    titre: str | None = None
    auteur: str | None = None
    dernier_modificateur: str | None = None
    nb_pages: int | None = None
    code_doc: str | None = None
    erreur: str | None = None


def famille(extension: str) -> str:
    return _FAMILLE_PAR_EXTENSION.get(extension.lower(), "Autre")


def extraire(chemin: Path, soffice: str | None = None) -> Resultat:
    """Extrait le texte d'un fichier. Ne lève pas d'exception : les erreurs sont dans le résultat."""
    ext = chemin.suffix.lower()
    try:
        if ext in (".docx", ".docm", ".dotx"):
            resultat = _docx(chemin)
        elif ext in (".xlsx", ".xlsm"):
            resultat = _xlsx(chemin)
        elif ext == ".pptx":
            resultat = _pptx(chemin)
        elif ext in (".odt", ".ods", ".odp"):
            resultat = _opendocument(chemin)
        elif ext == ".pdf":
            resultat = _pdf(chemin)
        elif ext == ".eml":
            resultat = _eml(chemin)
        elif ext in EXTENSIONS_TEXTE:
            resultat = _texte(chemin)
        elif ext in EXTENSIONS_OCR:
            resultat = Resultat(statut="ocr_requis")
        elif ext in EXTENSIONS_CONVERSION:
            resultat = _conversion(chemin, soffice)
        else:
            resultat = Resultat(statut="non_supporte")
    except Exception as exc:  # fichier corrompu, protégé par mot de passe, verrouillé...
        return Resultat(statut="erreur", erreur=f"{type(exc).__name__}: {exc}"[:500])

    if resultat.contenu and not resultat.code_doc:
        resultat.code_doc = detecter_code(resultat.contenu[:5000])
    return resultat


# --- Formats Office Open XML -------------------------------------------------


def _lire_xml(archive: zipfile.ZipFile, nom: str) -> ElementTree.Element | None:
    try:
        return ElementTree.fromstring(archive.read(nom))
    except KeyError:
        return None


def _proprietes(archive: zipfile.ZipFile) -> dict:
    """Titre, auteur et dernier modificateur depuis docProps/core.xml."""
    core = _lire_xml(archive, "docProps/core.xml")
    if core is None:
        return {}

    def valeur(balise: str) -> str | None:
        texte = core.findtext(balise)
        return (texte.strip() or None) if texte else None

    return {
        "titre": valeur(f"{NS_DC}title"),
        "auteur": valeur(f"{NS_DC}creator"),
        "dernier_modificateur": valeur(f"{NS_CP}lastModifiedBy"),
    }


def _paragraphes_word(racine: ElementTree.Element) -> list[str]:
    paragraphes = []
    for p in racine.iter(f"{NS_W}p"):
        morceaux = []
        for noeud in p.iter():
            if noeud.tag == f"{NS_W}t" and noeud.text:
                morceaux.append(noeud.text)
            elif noeud.tag == f"{NS_W}tab":
                morceaux.append("\t")
            elif noeud.tag in (f"{NS_W}br", f"{NS_W}cr"):
                morceaux.append("\n")
        texte = "".join(morceaux).strip()
        if texte:
            paragraphes.append(texte)
    return paragraphes


def _numero(nom: str) -> int:
    """'word/header12.xml' -> 12, pour trier dans l'ordre naturel."""
    trouve = re.search(r"(\d+)\.xml$", nom)
    return int(trouve.group(1)) if trouve else 0


def _docx(chemin: Path) -> Resultat:
    with zipfile.ZipFile(chemin) as archive:
        proprietes = _proprietes(archive)
        noms = archive.namelist()
        entetes = sorted((n for n in noms if re.match(r"word/(header|footer)\d*\.xml$", n)), key=_numero)

        texte_entetes = []
        for nom in entetes:
            racine = _lire_xml(archive, nom)
            if racine is not None:
                texte_entetes.extend(_paragraphes_word(racine))

        corps = _lire_xml(archive, "word/document.xml")
        paragraphes = _paragraphes_word(corps) if corps is not None else []

    # L'en-tête porte souvent le code et la version : on le place en tête du contenu
    contenu = "\n".join(_dedoublonner(texte_entetes) + paragraphes)
    return Resultat(statut="indexe", contenu=contenu, methode="texte", **proprietes)


def _xlsx(chemin: Path) -> Resultat:
    with zipfile.ZipFile(chemin) as archive:
        proprietes = _proprietes(archive)
        morceaux = []
        classeur = _lire_xml(archive, "xl/workbook.xml")
        if classeur is not None:
            morceaux.extend(f.get("name", "") for f in classeur.iter(f"{NS_S}sheet"))
        chaines = _lire_xml(archive, "xl/sharedStrings.xml")
        if chaines is not None:
            for si in chaines.iter(f"{NS_S}si"):
                texte = "".join(t.text or "" for t in si.iter(f"{NS_S}t")).strip()
                if texte:
                    morceaux.append(texte)
    return Resultat(statut="indexe", contenu="\n".join(morceaux), methode="texte", **proprietes)


def _pptx(chemin: Path) -> Resultat:
    with zipfile.ZipFile(chemin) as archive:
        proprietes = _proprietes(archive)
        diapos = sorted(
            (n for n in archive.namelist() if re.match(r"ppt/(slides/slide|notesSlides/notesSlide)\d+\.xml$", n)),
            key=lambda n: ("notes" in n, _numero(n)),
        )
        morceaux = []
        for nom in diapos:
            racine = _lire_xml(archive, nom)
            if racine is None:
                continue
            for p in racine.iter(f"{NS_A}p"):
                texte = "".join(t.text or "" for t in p.iter(f"{NS_A}t")).strip()
                if texte:
                    morceaux.append(texte)
    return Resultat(
        statut="indexe", contenu="\n".join(morceaux), methode="texte", **proprietes,
        nb_pages=sum(1 for n in diapos if "notes" not in n),
    )


def _opendocument(chemin: Path) -> Resultat:
    with zipfile.ZipFile(chemin) as archive:
        racine = _lire_xml(archive, "content.xml")
    if racine is None:
        return Resultat(statut="indexe", contenu="", methode="texte")
    morceaux = []
    for noeud in racine.iter():
        if noeud.tag in (f"{NS_TEXT_ODF}p", f"{NS_TEXT_ODF}h"):
            texte = "".join(noeud.itertext()).strip()
            if texte:
                morceaux.append(texte)
    return Resultat(statut="indexe", contenu="\n".join(morceaux), methode="texte")


# --- PDF ----------------------------------------------------------------------


def _pdf(chemin: Path) -> Resultat:
    import pymupdf

    # Messages de MuPDF sur la console : ils casseraient la barre de progression. Les vraies
    # erreurs lèvent une exception et sont enregistrées dans le document.
    pymupdf.TOOLS.mupdf_display_errors(False)

    with pymupdf.open(chemin) as doc:
        if doc.needs_pass:
            return Resultat(statut="erreur", erreur="PDF protégé par mot de passe")
        pages = [page.get_text() for page in doc]
        meta = doc.metadata or {}

    contenu = "\n".join(p.strip() for p in pages if p.strip())
    nb_pages = len(pages)
    scanne = nb_pages > 0 and len(contenu) < CARACTERES_MIN_PAR_PAGE * nb_pages
    return Resultat(
        statut="ocr_requis" if scanne else "indexe",
        contenu=contenu,
        methode="texte",
        titre=(meta.get("title") or "").strip() or None,
        auteur=(meta.get("author") or "").strip() or None,
        nb_pages=nb_pages,
    )


# --- Texte brut et mails --------------------------------------------------------


def decoder(octets: bytes) -> str:
    """UTF-8 si possible, sinon Windows-1252 (encodage historique des postes Windows)."""
    try:
        return octets.decode("utf-8-sig")
    except UnicodeDecodeError:
        return octets.decode("cp1252", errors="replace")


def _texte(chemin: Path) -> Resultat:
    with chemin.open("rb") as f:
        octets = f.read(TAILLE_MAX_TEXTE)
    return Resultat(statut="indexe", contenu=decoder(octets), methode="texte")


def _eml(chemin: Path) -> Resultat:
    message = email.message_from_bytes(chemin.read_bytes(), policy=email.policy.default)
    corps = message.get_body(preferencelist=("plain", "html"))
    texte = corps.get_content() if corps is not None else ""
    if corps is not None and corps.get_content_type() == "text/html":
        texte = re.sub(r"<[^>]+>", " ", texte)
    entete = f"De : {message['from'] or ''}\nÀ : {message['to'] or ''}\nObjet : {message['subject'] or ''}"
    return Resultat(
        statut="indexe", contenu=f"{entete}\n\n{texte.strip()}", methode="texte",
        titre=str(message["subject"] or "") or None, auteur=str(message["from"] or "") or None,
    )


# --- Anciens formats via LibreOffice ---------------------------------------------


# Sans ces variables, LibreOffice interroge le spouleur Windows à chaque démarrage pour connaître
# l'imprimante par défaut (notification "connexion à l'imprimante"). Inutile pour une conversion.
_ENV_LIBREOFFICE = {**os.environ, "SAL_DISABLE_PRINTERLIST": "1", "SAL_DISABLE_DEFAULTPRINTER": "1"}


def _conversion(chemin: Path, soffice: str | None) -> Resultat:
    if not soffice or not Path(soffice).is_file():
        return Resultat(statut="non_supporte", erreur="Ancien format : LibreOffice non configuré")

    with tempfile.TemporaryDirectory(prefix="ged_") as dossier:
        # LibreOffice travaille sur une copie locale : il pose un fichier verrou (.~lock.xxx#)
        # à côté du document qu'il ouvre, et le laisse en place s'il plante ou est interrompu
        entree = Path(dossier, "entree")
        entree.mkdir()
        copie = Path(shutil.copyfile(chemin, entree / chemin.name))
        sortie = Path(dossier, "sortie")
        # Profil LibreOffice dédié : évite les conflits avec une instance ouverte par l'utilisateur
        profil = Path(dossier, "profil").as_uri()
        _lancer_libreoffice(
            [soffice, f"-env:UserInstallation={profil}", "--headless", "--norestore",
             "--convert-to", "pdf", "--outdir", str(sortie), str(copie)]
        )
        pdf = Path(sortie, chemin.stem + ".pdf")
        if not pdf.is_file():
            return Resultat(statut="erreur", erreur="Conversion LibreOffice sans résultat")
        resultat = _pdf(pdf)

    resultat.methode = "conversion"
    return resultat


DELAI_CONVERSION = 180  # secondes


def _lancer_libreoffice(commande: list[str]) -> None:
    """Lance LibreOffice et l'arrête complètement s'il dépasse le délai.

    Sous Windows, soffice.exe démarre soffice.bin, qui fait le vrai travail : tuer soffice.exe
    seul (ce que fait subprocess.run en cas de délai dépassé) laisserait soffice.bin tourner.
    """
    processus = subprocess.Popen(
        commande, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_ENV_LIBREOFFICE
    )
    try:
        _, erreurs = processus.communicate(timeout=DELAI_CONVERSION)
    except subprocess.TimeoutExpired:
        if sys.platform == "win32":
            # /T : tout l'arbre de processus
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(processus.pid)], capture_output=True)
        processus.kill()
        processus.communicate()
        raise
    if processus.returncode:
        raise subprocess.CalledProcessError(processus.returncode, commande, stderr=erreurs)


def _dedoublonner(lignes: list[str]) -> list[str]:
    """Les en-têtes sont souvent répétés (première page, pages paires...) : on garde chaque ligne une fois."""
    vues: set[str] = set()
    resultat = []
    for ligne in lignes:
        if ligne not in vues:
            vues.add(ligne)
            resultat.append(ligne)
    return resultat
