import os
from pathlib import Path

from ged.extraits import construire_motif, extraire
from ged.indexeur import a_quitte, parcourir
from ged.recherche import Filtres, requete_saisie, sous_titre


def creer(racine: Path, *chemins: str) -> None:
    for c in chemins:
        f = racine / c
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")


def test_parcourir_ignore_bruit_systeme_et_sensible(tmp_path):
    creer(
        tmp_path,
        "Infra/note.docx",
        "Infra/~$note.docx",
        "Infra/Thumbs.db",
        "Infra/.stignore",
        "Infra/.stfolder/marqueur",
        "Infra/raccourci.lnk",
        "Infra/#recycle/supprime.docx",
        "Infra/@eaDir/vignette.jpg",
        "Infra/.stversions/ancienne.docx",
        "Infra/Keepass/coffre.kdbx",
        "Infra/certificats/serveur.pfx",
        "Metier/Archives/vieux.pdf",
        "Metier/procedure.pdf",
    )
    trouves = {p.relative_to(tmp_path).as_posix() for p in parcourir(tmp_path, exclusions=["*\\Archives\\*", "*/Archives/*"])}
    assert trouves == {"Infra/note.docx", "Metier/procedure.pdf"}


def test_parcourir_saute_dossiers_exclus_et_binaires(tmp_path, monkeypatch):
    creer(
        tmp_path,
        "Applis/LogicielX/bin/moteur.dll",
        "Applis/LogicielX/doc/manuel.pdf",
        "Applis/LogicielY/installeur.exe",
        "Applis/LogicielY/lisezmoi.txt",
    )
    visites = []
    original = os.walk
    monkeypatch.setattr(os, "walk", lambda racine, **kw: ((visites.append(d) or (d, s, f)) for d, s, f in original(racine, **kw)))
    motif = str(tmp_path / "applis" / "logicielx")
    trouves = {p.relative_to(tmp_path).as_posix() for p in parcourir(tmp_path, exclusions=[motif])}
    assert trouves == {"Applis/LogicielY/lisezmoi.txt"}
    assert not any("LogicielX" in d for d in visites)


def test_requete_saisie_dernier_mot_en_prefixe():
    assert requete_saisie(["imprimante", "bloq"]) == "imprimante & bloq:*"
    assert requete_saisie([]) is None


def test_sous_titre_seulement_s_il_apporte_une_information():
    assert sous_titre("NI ASSUREPONSE IMMO", "Simulation CA.pdf") == "NI ASSUREPONSE IMMO"
    assert sous_titre("Procédure VPN", "Procedure VPN v2.docx") is None
    assert sous_titre(None, "x.pdf") is None


def test_lien_de_pagination():
    f = Filtres(q="vpn bloqué", pole="Infra", tags=[3, 7], page=2)
    assert f.lien(3) == "q=vpn+bloqu%C3%A9&pole=Infra&tag=3&tag=7&page=3"


def test_extrait_echappe_le_html_et_surligne_sans_accents():
    texte = "<script>alert(1)</script> Les procédures de redémarrage du spouleur. " + "bla " * 200 + "Fin."
    resultat = extraire(texte, construire_motif(["procedure", "redemarr"]))
    assert "<script>" not in resultat
    assert "&lt;script&gt;" in resultat
    # Racine du mot : trouve le pluriel et les accents
    assert "<mark>procédures</mark>" in resultat
    assert "<mark>redémarrage</mark>" in resultat


def test_extrait_sans_correspondance_donne_le_debut():
    assert str(extraire("Début du texte. " * 50, construire_motif(["absent"]))).startswith("Début du texte.")
    assert extraire(None, None) == ""


def test_parcourir_signale_les_dossiers_inaccessibles(tmp_path):
    inaccessibles: list[Path] = []
    assert list(parcourir(tmp_path / "absent", [], inaccessibles)) == []
    assert inaccessibles == [tmp_path / "absent"]


def test_deplacement_seulement_si_l_ancien_fichier_a_disparu(tmp_path):
    present = tmp_path / "Passeport.pdf"
    present.write_bytes(b"x")
    nouveau = str(tmp_path / "Identite" / "Passeport Antoine.pdf")
    # Toujours là : c'est un doublon, pas un déplacement
    assert not a_quitte(str(present), nouveau)
    assert a_quitte(str(tmp_path / "ancien nom.pdf"), nouveau)
    # Changement de casse seule : l'ancien chemin « existe » encore sous Windows
    assert a_quitte(str(present), str(tmp_path / "passeport.pdf"))


def test_periode_bornes_incluses_par_annee_ou_par_mois():
    from datetime import date

    assert Filtres(du="2020", au="2022").periode() == (date(2020, 1, 1), date(2023, 1, 1))
    assert Filtres(du="2021-03", au="2021-12").periode() == (date(2021, 3, 1), date(2022, 1, 1))
    assert Filtres(du="n'importe", au="2021-13").periode() == (None, None)
    assert "du=2020&au=2021-03" in Filtres(du="2020", au="2021-03").lien()


def test_ctrl_c_pendant_l_ocr_n_est_pas_une_erreur_du_document(tmp_path, monkeypatch):
    import pymupdf
    import pytest

    from ged.ocr import ocr

    (tmp_path / "fra.traineddata").write_bytes(b"")

    def interrompu(*args, **kwargs):
        # Ce que MuPDF lève quand Ctrl+C arrive pendant la reconnaissance
        raise RuntimeError("code=1: Director error: <class 'KeyboardInterrupt'>: Traceback...")

    monkeypatch.setattr(pymupdf, "open", interrompu)
    with pytest.raises(KeyboardInterrupt):
        ocr(tmp_path / "scan.pdf", str(tmp_path))

    def illisible(*args, **kwargs):
        raise RuntimeError("fichier corrompu")

    monkeypatch.setattr(pymupdf, "open", illisible)
    assert ocr(tmp_path / "scan.pdf", str(tmp_path)).statut == "erreur"


def test_reessayer_l_ocr_des_documents_en_erreur(tmp_path, conn, monkeypatch):
    from ged import indexeur
    from ged.config import Config
    from ged.extraction import Resultat

    source = conn.execute("INSERT INTO source (nom, chemin, pole) VALUES ('S', 'x', 'S') RETURNING id").fetchone()["id"]
    for nom, statut, erreur in (("attente.pdf", "ocr_requis", None), ("echec.pdf", "erreur", "OCR : interrompu"),
                                ("protege.pdf", "erreur", "PDF protégé par mot de passe")):
        conn.execute(
            "INSERT INTO document (source_id, chemin, chemin_relatif, nom, extension, famille, taille, modifie_le, "
            "statut, erreur) VALUES (%s, %s, %s, %s, '.pdf', 'PDF', 1, now(), %s, %s)",
            (source, rf"x\{nom}", nom, nom, statut, erreur),
        )
    lus = []

    def faux_ocr(chemin, *args, **kwargs):
        lus.append(chemin.name)
        return Resultat(statut="indexe", contenu="texte lu", methode="ocr", nb_pages=1)

    monkeypatch.setattr(indexeur, "ocr", faux_ocr)
    cfg = Config(url_base="", sources=[], tessdata=str(tmp_path))
    indexeur.Indexeur(cfg, conn).ocr()
    assert lus == ["attente.pdf"]
    indexeur.Indexeur(cfg, conn).ocr(reessayer=True)
    # Seule l'erreur d'OCR est relancée, pas le PDF protégé (illisible quoi qu'il arrive)
    assert lus == ["attente.pdf", "echec.pdf"]
