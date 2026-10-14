import zipfile
from pathlib import Path

import pymupdf
import pytest

from ged.extraction import extraire, famille
from ged.texte import code_document, detecter_code


def test_docx_texte_entete_et_proprietes(fabriquer_docx):
    chemin = fabriquer_docx(
        "note.docx",
        ["Objet", "Redémarrer le spouleur d'impression."],
        entete="Note technique DSI-INFRA-NT-001-v02",
        titre="Débug imprimantes",
        auteur="Tech A",
    )
    r = extraire(chemin)
    assert r.statut == "indexe"
    assert r.titre == "Débug imprimantes"
    assert r.auteur == "Tech A"
    assert r.dernier_modificateur == "Tech B"
    assert r.code_doc == "DSI-INFRA-NT-001-v02"
    # L'en-tête, présent dans deux fichiers header, n'apparaît qu'une fois, en tête du contenu
    assert r.contenu.count("Note technique") == 1
    assert r.contenu.splitlines()[0].startswith("Note technique")
    assert "spouleur" in r.contenu


def test_xlsx_chaines_et_onglets(tmp_path):
    chemin = tmp_path / "inventaire.xlsx"
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    with zipfile.ZipFile(chemin, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook {ns}><sheets><sheet name="Serveurs"/></sheets></workbook>')
        z.writestr("xl/sharedStrings.xml", f"<sst {ns}><si><t>SRV-AD-01</t></si><si><r><t>Contrôleur </t></r><r><t>de domaine</t></r></si></sst>")
    r = extraire(chemin)
    assert r.statut == "indexe"
    assert r.contenu.splitlines() == ["Serveurs", "SRV-AD-01", "Contrôleur de domaine"]


def test_pptx_diapositives_dans_l_ordre(tmp_path):
    chemin = tmp_path / "formation.pptx"
    ns = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    with zipfile.ZipFile(chemin, "w") as z:
        for i in (10, 2, 1):
            z.writestr(f"ppt/slides/slide{i}.xml", f"<sld {ns}><a:p><a:r><a:t>Diapo {i}</a:t></a:r></a:p></sld>")
    r = extraire(chemin)
    assert r.contenu.splitlines() == ["Diapo 1", "Diapo 2", "Diapo 10"]
    assert r.nb_pages == 3


def _pdf(chemin, textes):
    doc = pymupdf.open()
    for texte in textes:
        page = doc.new_page()
        if texte:
            page.insert_text((72, 72), texte)
    doc.save(chemin)


def test_pdf_texte(tmp_path):
    chemin = tmp_path / "procedure.pdf"
    _pdf(chemin, ["Procedure de connexion au VPN depuis le domicile, etape par etape."])
    r = extraire(chemin)
    assert r.statut == "indexe"
    assert "VPN" in r.contenu
    assert r.nb_pages == 1


def test_pdf_sans_texte_part_en_ocr(tmp_path):
    chemin = tmp_path / "scan.pdf"
    _pdf(chemin, ["", ""])
    r = extraire(chemin)
    assert r.statut == "ocr_requis"
    assert r.nb_pages == 2


def test_texte_windows_1252(tmp_path):
    chemin = tmp_path / "notes.txt"
    chemin.write_bytes("Procédure d'arrêt".encode("cp1252"))
    assert extraire(chemin).contenu == "Procédure d'arrêt"


def test_eml(tmp_path):
    chemin = tmp_path / "incident.eml"
    chemin.write_text(
        "From: support@exemple.fr\nTo: dsi@exemple.fr\nSubject: Panne messagerie\n"
        "Content-Type: text/plain; charset=utf-8\n\nRedemarrer le service de transport.\n",
        encoding="utf-8",
    )
    r = extraire(chemin)
    assert r.titre == "Panne messagerie"
    assert "Redemarrer le service" in r.contenu


def test_image_part_en_ocr(tmp_path):
    chemin = tmp_path / "capture.png"
    chemin.write_bytes(b"\x89PNG")
    assert extraire(chemin).statut == "ocr_requis"


def test_ancien_format_sans_libreoffice(tmp_path):
    chemin = tmp_path / "vieux.doc"
    chemin.write_bytes(b"\xd0\xcf\x11\xe0")
    r = extraire(chemin, soffice=None)
    assert r.statut == "non_supporte"


def test_fichier_corrompu_ne_plante_pas(tmp_path):
    chemin = tmp_path / "abime.docx"
    chemin.write_bytes(b"ceci n'est pas un zip")
    r = extraire(chemin)
    assert r.statut == "erreur"
    assert "BadZipFile" in r.erreur


@pytest.mark.parametrize(
    "extension, attendu",
    [(".DOCX", "Word"), (".pdf", "PDF"), (".jpeg", "Image"), (".kdbx", "Autre"), (".mov", "Video")],
)
def test_famille(extension, attendu):
    assert famille(extension) == attendu


@pytest.mark.parametrize(
    "texte, attendu",
    [
        ("Réf : DSI-INFRA-PRO-012-v03 page 1", "DSI-INFRA-PRO-012-v03"),
        ("code DSI-METIER-NT-7 incomplet", None),
        ("DSI-BI-MO-0042", "DSI-BI-MO-0042"),
        # Faux positif constaté sur un acte notarié : une date n'est pas un code
        ("Réf. SOLID-20-10-20", None),
        # Séparateurs "." et "_"
        ("NT.OC.GSI.01  procédure de connexion.docx", "NT.OC.GSI.01"),
        ("scan_NT_OC_GSI_01_procedure.pdf", "NT_OC_GSI_01"),
        ("PR.DSI.INFRA.003.V2.pdf", "PR.DSI.INFRA.003.V2"),
        ("version 2.1.3 du logiciel", None),
    ],
)
def test_detecter_code(texte, attendu):
    assert detecter_code(texte) == attendu


def test_code_document_cherche_d_abord_dans_le_nom():
    assert code_document(r"Procedures\NT.OC.GSI.01  procédure.docx", "Réf : DSI-INFRA-PRO-012") == "NT.OC.GSI.01"
    assert code_document(r"Procedures\procédure.docx", "Réf : DSI-INFRA-PRO-012") == "DSI-INFRA-PRO-012"
    # Un dossier codé ne donne pas son code aux fichiers qu'il contient
    assert code_document(r"NT.OC.GSI.01\annexe.docx", "") is None


def test_resolution_ocr_plafonnee_pour_les_photos():
    from ged.ocr import resolution_effective

    # Page A4 (595 x 842 pt) : 300 dpi conservés
    assert resolution_effective(595, 842, 300) == 300
    # Photo 4000 x 3000 px vue à 72 dpi : page de 55 pouces, rendue à environ 4000 px de large
    assert resolution_effective(4000, 3000, 300) == 72


def test_texte_ocr_exploitable():
    from ged.ocr import texte_exploitable

    vocabulaire = {f"mot{i}" for i in range(2000)} | {"facture", "pharmacie", "client", "date", "numero"}
    assert texte_exploitable("Facture pharmacie\nNuméro client 1234 Date 16/09/2025", vocabulaire)
    # Bruit typique d'une photo passée en OCR
    assert not texte_exploitable("= Aa 7 RAR ÉAE LORS ÈS EX ÉNesN PR etALES\nee ET | ; te", vocabulaire)
    # Vocabulaire trop petit pour juger : on garde tout texte non vide
    assert texte_exploitable("ee ET", {"facture"})


def test_conversion_sur_une_copie_hors_du_partage(tmp_path, monkeypatch):
    import ged.extraction as extraction

    partage = tmp_path / "partage"
    partage.mkdir()
    chemin = partage / "vieux.doc"
    chemin.write_bytes(b"\xd0\xcf\x11\xe0")
    soffice = tmp_path / "soffice.exe"
    soffice.write_bytes(b"")
    recus = []

    def faux_libreoffice(commande):
        entree, sortie = Path(commande[-1]), Path(commande[commande.index("--outdir") + 1])
        recus.append(entree)
        sortie.mkdir()
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((72, 72), "Procédure de redémarrage du serveur " * 3)
            pdf.save(sortie / "vieux.pdf")

    monkeypatch.setattr(extraction, "_lancer_libreoffice", faux_libreoffice)
    r = extraire(chemin, soffice=str(soffice))
    assert r.methode == "conversion" and "redémarrage" in r.contenu
    assert recus[0].name == "vieux.doc" and recus[0].parent != partage
    assert [p.name for p in partage.iterdir()] == ["vieux.doc"]


def test_libreoffice_arrete_au_dela_du_delai(monkeypatch):
    import subprocess
    import sys

    import ged.extraction as extraction

    monkeypatch.setattr(extraction, "DELAI_CONVERSION", 1)
    with pytest.raises(subprocess.TimeoutExpired):
        extraction._lancer_libreoffice([sys.executable, "-c", "import time; time.sleep(30)"])
