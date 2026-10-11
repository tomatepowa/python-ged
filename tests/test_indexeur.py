from pathlib import Path

from ged.config import charger
from ged.indexeur import a_quitte, parcourir
from ged.extraits import construire_motif, extraire
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


def test_config_exemple_valide():
    racine = Path(__file__).resolve().parents[1]
    cfg = charger(racine / "config.exemple.toml")
    assert [s.pole for s in cfg.sources] == ["Métier", "Infra"]
    assert str(cfg.sources[0].chemin) == r"\\nas\metier\ged"
    assert cfg.taxonomie == racine / "taxonomie.toml"
    assert cfg.exemples_recherche == "imprimante, identitovigilance, \"message d'erreur\""


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
