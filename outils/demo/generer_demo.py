"""Génère un jeu de documents fictifs pour la démonstration et les captures d'écran.

Usage : uv run python outils/demo/generer_demo.py <dossier_cible>

Crée deux sources (GED Infra, GED Métier) avec des notes techniques et procédures inventées :
.docx et .xlsx écrits à la main (Office Open XML minimal), .pdf via PyMuPDF. Noms, auteurs et
logiciels métier (« DPI Hélios », « Facturix ») sont fictifs.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import pymupdf

# chemin relatif -> (titre, auteur, code documentaire ou "", paragraphes)
DOCUMENTS: dict[str, tuple[str, str, str, list[str]]] = {
    # --- GED Infra -------------------------------------------------------------------------
    "GED Infra/Réseau/VPN/Connexion VPN FortiClient impossible.docx": (
        "Connexion VPN FortiClient impossible", "Julien Marchand", "DSI-INFRA-NT-012-v02", [
            "Note technique – astreinte. Symptôme : le client FortiClient affiche le message d'erreur "
            "« Unable to establish the VPN connection » (code -14) depuis le domicile.",
            "Vérifier que l'utilisateur est bien dans le groupe VPN-Nomades de l'Active Directory.",
            "Contournement : supprimer puis recréer le profil VPN, redémarrer le service FortiClient.",
            "Si le VPN ne fonctionne pas après redémarrage, vérifier le certificat du portail sur le pare-feu.",
        ]),
    "GED Infra/Réseau/VPN/Procédure installation FortiClient poste nomade.pdf": (
        "Procédure installation FortiClient", "Julien Marchand", "DSI-INFRA-PRO-004-v03", [
            "Mode opératoire d'installation du client VPN FortiClient sur un poste nomade.",
            "1. Télécharger le paquet depuis le partage logiciels. 2. Installer en administrateur.",
            "3. Importer le profil VPN de l'établissement. 4. Tester la connexion VPN depuis l'extérieur.",
        ]),
    "GED Infra/Réseau/Pare-feu/Ouverture de flux pare-feu - mode opératoire.docx": (
        "Ouverture de flux pare-feu", "Nadia Ferreira", "DSI-INFRA-PRO-009-v01", [
            "Mode opératoire pour ouvrir un flux sur le pare-feu du site principal.",
            "Toute demande passe par un ticket EasyVista validé par le responsable infrastructure.",
            "Créer l'objet, la règle dans le bon VLAN, puis journaliser la modification du pare-feu.",
        ]),
    "GED Infra/Serveurs/Sauvegardes/Procédure restauration de fichiers.pdf": (
        "Procédure restauration de fichiers", "Nadia Ferreira", "DSI-INFRA-PRO-015-v02", [
            "Restaurer un fichier supprimé sur un partage à partir de la sauvegarde de la nuit.",
            "Les restaurations de plus de 30 jours nécessitent la baie de stockage secondaire.",
        ]),
    "GED Infra/Serveurs/VMware/Redémarrage d'un hôte ESXi en astreinte.docx": (
        "Redémarrage d'un hôte ESXi", "Thomas Lemaire", "DSI-INFRA-NT-021-v01", [
            "Note technique : un hôte ESXi ne répond plus dans vCenter.",
            "Migrer les machines virtuelles avec vMotion avant tout redémarrage de l'hôte VMware.",
            "Contournement si vCenter est indisponible : se connecter directement à l'hôte ESXi.",
        ]),
    "GED Infra/Impression/Debug imprimantes - file d'impression bloquée.docx": (
        "File d'impression bloquée", "Thomas Lemaire", "DSI-INFRA-NT-003-v04", [
            "Debug utilisateur : les documents restent bloqués dans la file d'impression.",
            "Redémarrer le service spouleur sur le serveur d'impression, puis vider la file d'impression.",
            "Si l'imprimante ne fonctionne pas toujours, vérifier le pilote et l'adresse IP de l'imprimante.",
        ]),
    "GED Infra/Impression/Copieurs/Liste des copieurs par site.xlsx": (
        "Liste des copieurs", "Thomas Lemaire", "", [
            "Site", "Étage", "Modèle", "Adresse IP", "Clinique du Parc", "Clinique des Oliviers",
            "Copieur couleur A3", "Imprimante étiquettes", "Copieur noir et blanc",
        ]),
    "GED Infra/Citrix/Session Citrix figée - contournement.docx": (
        "Session Citrix figée", "Julien Marchand", "DSI-INFRA-NT-017-v01", [
            "Debug utilisateur : la session Citrix ne répond plus et l'écran reste figé.",
            "Contournement : fermer la session depuis la console Citrix Studio, puis relancer via StoreFront.",
        ]),
    "GED Infra/Citrix/StoreFront/Note technique certificat StoreFront.pdf": (
        "Certificat StoreFront", "Nadia Ferreira", "DSI-INFRA-NT-018-v02", [
            "Note technique : renouvellement annuel du certificat du serveur StoreFront Citrix.",
            "Message d'erreur côté utilisateur : « Impossible de se connecter au serveur ».",
        ]),
    "GED Infra/Comptes/Active Directory/Déverrouillage d'un compte AD.docx": (
        "Déverrouillage d'un compte", "Sophie Garnier", "DSI-INFRA-PRO-002-v05", [
            "Procédure de déverrouillage d'un compte dans l'Active Directory.",
            "Vérifier l'identité de l'appelant avant toute réinitialisation de mot de passe.",
            "Les comptes de service ne se déverrouillent pas sans l'accord de l'équipe Active Directory.",
        ]),
    "GED Infra/Comptes/Active Directory/GPO/Note technique GPO écran de veille.docx": (
        "GPO écran de veille", "Sophie Garnier", "DSI-INFRA-NT-030-v01", [
            "Note technique : la GPO de verrouillage d'écran s'applique aux postes des services de soins.",
            "Exception pour les postes de bloc opératoire : GPO dédiée sur l'unité d'organisation Bloc.",
        ]),
    "GED Infra/Messagerie/Boîte mail pleine dans Outlook.docx": (
        "Boîte mail pleine", "Sophie Garnier", "DSI-INFRA-NT-008-v02", [
            "Debug utilisateur : message d'erreur « Votre boîte aux lettres est pleine » dans Outlook.",
            "Archiver les anciens messages ou demander une extension de quota Exchange via EasyVista.",
        ]),
    "GED Infra/Archives/2019/Réseau/Wi-Fi/Ancienne procédure Wi-Fi invités.pdf": (
        "Ancienne procédure Wi-Fi invités", "Julien Marchand", "DSI-INFRA-PRO-001-v01", [
            "Procédure obsolète conservée pour mémoire : le Wi-Fi invités est désormais géré par le portail captif.",
        ]),
    # --- GED Métier ------------------------------------------------------------------------
    "GED Métier/DPI Hélios/Identitovigilance/Fusion de doublons patients.docx": (
        "Fusion de doublons patients", "Claire Dubois", "DSI-METIER-PRO-011-v03", [
            "Procédure d'identitovigilance : fusion de doublons patients dans le DPI Hélios.",
            "Ne jamais fusionner sans validation de la cellule d'identitovigilance.",
            "Contrôler l'identifiant national de santé (INS) des deux dossiers avant la fusion.",
        ]),
    "GED Métier/DPI Hélios/Identitovigilance/Procédure création d'identité INS.pdf": (
        "Création d'identité INS", "Claire Dubois", "DSI-METIER-PRO-012-v02", [
            "Mode opératoire : création d'une identité patient qualifiée avec l'identifiant national de santé.",
            "L'identitovigilance impose la vérification d'une pièce d'identité à l'admission.",
        ]),
    "GED Métier/DPI Hélios/Plan de soins/Debug administration médicamenteuse.docx": (
        "Administration médicamenteuse", "Marc Henry", "DSI-METIER-NT-025-v01", [
            "Debug utilisateur : l'administration médicamenteuse ne s'enregistre pas dans le plan de soins.",
            "Contournement : saisir l'administration depuis le dossier de soins, onglet traçabilité.",
            "Vérifier que la prescription est validée par le pharmacien dans le DPI.",
        ]),
    "GED Métier/Facturation/Facturix/Rejets NOEMIE - mode opératoire.docx": (
        "Rejets NOEMIE", "Isabelle Roux", "DSI-METIER-PRO-031-v02", [
            "Mode opératoire de traitement des rejets NOEMIE dans le logiciel de facturation Facturix.",
            "Chaque rejet de facturation est corrigé puis renvoyé dans le lot du lendemain.",
        ]),
    "GED Métier/Facturation/Facturix/Titres de recette - erreur d'export.pdf": (
        "Titres de recette – erreur d'export", "Isabelle Roux", "DSI-METIER-NT-033-v01", [
            "Note technique : l'export des titres de recette vers la comptabilité échoue.",
            "Message d'erreur : « Fichier de facturation verrouillé ». Relancer l'export après 22 h.",
        ]),
    "GED Métier/Pharmacie/Dispensation - interface automate.docx": (
        "Interface automate de dispensation", "Marc Henry", "DSI-METIER-NT-040-v01", [
            "Note technique : l'interface entre le DPI et l'automate de dispensation est coupée.",
            "Redémarrer le connecteur ; la dispensation reprend avec les prescriptions en attente.",
        ]),
    "GED Métier/Imagerie/PACS/Images DICOM non reçues.docx": (
        "Images DICOM non reçues", "Marc Henry", "DSI-METIER-NT-044-v02", [
            "Debug utilisateur : les images du scanner n'arrivent pas dans le PACS.",
            "Vérifier la file d'envoi DICOM de la modalité et l'espace disque du PACS.",
        ]),
    "GED Métier/RH/Gestion des temps/Badgeuse ne fonctionne pas.docx": (
        "Badgeuse hors service", "Isabelle Roux", "DSI-METIER-NT-050-v01", [
            "Debug utilisateur : la badgeuse du hall ne fonctionne pas.",
            "Les pointages sont repris dans la gestion des temps à la reconnexion de la badgeuse.",
        ]),
    "GED Métier/Laboratoire/Résultats de biologie absents du DPI.pdf": (
        "Résultats de biologie absents", "Claire Dubois", "DSI-METIER-NT-055-v01", [
            "Note technique : les résultats du laboratoire n'apparaissent plus dans le DPI Hélios.",
            "Contrôler le flux HPRIM entre le laboratoire et le DPI, puis relancer les messages en erreur.",
        ]),
    "GED Métier/EasyVista/Modèle de ticket incident.docx": (
        "Modèle de ticket incident", "Sophie Garnier", "", [
            "Modèle de ticket EasyVista pour un incident déclaré en astreinte.",
            "Renseigner le service, le logiciel concerné, le message d'erreur exact et le lien vers la note technique.",
        ]),
}

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/{principal}" ContentType="{type}"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
</Types>"""

RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="{principal}"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
</Relationships>"""

CORE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{titre}</dc:title><dc:creator>{auteur}</dc:creator>
<cp:lastModifiedBy>{auteur}</cp:lastModifiedBy></cp:coreProperties>"""

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _ecrire_paquet(chemin: Path, principal: str, type_principal: str, titre: str, auteur: str,
                   parties: dict[str, str]) -> None:
    with zipfile.ZipFile(chemin, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES.format(principal=principal, type=type_principal))
        z.writestr("_rels/.rels", RELS.format(principal=principal))
        z.writestr("docProps/core.xml", CORE.format(titre=escape(titre), auteur=escape(auteur)))
        for nom, xml in parties.items():
            z.writestr(nom, xml)


def _paragraphes(textes: list[str]) -> str:
    return "".join(f'<w:p><w:r><w:t xml:space="preserve">{escape(t)}</w:t></w:r></w:p>' for t in textes)


def docx(chemin: Path, titre: str, auteur: str, code: str, paragraphes: list[str]) -> None:
    parties = {"word/document.xml": f'<w:document xmlns:w="{W}"><w:body>{_paragraphes([titre, *paragraphes])}</w:body></w:document>'}
    if code:
        # Le code et la version sont dans l'en-tête, comme dans le modèle de note de la DSI
        parties["word/header1.xml"] = f'<w:hdr xmlns:w="{W}">{_paragraphes([code])}</w:hdr>'
    _ecrire_paquet(chemin, "word/document.xml",
                   "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
                   titre, auteur, parties)


def xlsx(chemin: Path, titre: str, auteur: str, cellules: list[str]) -> None:
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    chaines = "".join(f"<si><t>{escape(c)}</t></si>" for c in cellules)
    _ecrire_paquet(chemin, "xl/workbook.xml",
                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml", titre, auteur, {
                       "xl/workbook.xml": f'<workbook xmlns="{ns}"><sheets><sheet name="Copieurs" sheetId="1"/></sheets></workbook>',
                       "xl/sharedStrings.xml": f'<sst xmlns="{ns}">{chaines}</sst>',
                   })


def pdf(chemin: Path, titre: str, auteur: str, code: str, paragraphes: list[str]) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    texte = "\n\n".join(filter(None, [code, titre, *paragraphes]))
    page.insert_textbox(pymupdf.Rect(60, 60, 540, 780), texte, fontsize=11, fontname="helv")
    doc.set_metadata({"title": titre, "author": auteur})
    doc.save(chemin)
    doc.close()


def generer(cible: Path) -> None:
    for relatif, (titre, auteur, code, paragraphes) in DOCUMENTS.items():
        chemin = cible / relatif
        chemin.parent.mkdir(parents=True, exist_ok=True)
        if chemin.suffix == ".docx":
            docx(chemin, titre, auteur, code, paragraphes)
        elif chemin.suffix == ".xlsx":
            xlsx(chemin, titre, auteur, paragraphes)
        else:
            pdf(chemin, titre, auteur, code, paragraphes)
    print(f"{len(DOCUMENTS)} documents fictifs créés dans {cible}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    generer(Path(sys.argv[1]))
