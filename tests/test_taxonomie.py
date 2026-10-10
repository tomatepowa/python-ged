from pathlib import Path

from ged.taxonomie import Tag, Taxonomie

RACINE = Path(__file__).resolve().parents[1]


def taxonomie(tmp_path, contenu: str) -> Taxonomie:
    fichier = tmp_path / "taxonomie.toml"
    fichier.write_text(contenu, encoding="utf-8")
    return Taxonomie.charger(fichier)


def test_accents_et_casse_ignores(tmp_path):
    t = taxonomie(tmp_path, 'seuil = 1\n[secteur]\n"Identité" = ["identitovigilance"]\n')
    assert t.proposer("", "Rappel : IDENTITOVIGILANCE obligatoire") == [Tag("secteur", "Identité")]


def test_mot_entier_uniquement(tmp_path):
    t = taxonomie(tmp_path, 'seuil = 1\n[secteur]\n"Identité" = ["ins"]\n')
    assert t.proposer("", "instance installée") == []
    assert t.proposer("", "Vérifier l'INS du patient") == [Tag("secteur", "Identité")]


def test_seuil_et_poids_du_nom(tmp_path):
    t = taxonomie(tmp_path, 'seuil = 2\n[logiciel]\n"VPN" = ["vpn"]\n')
    # Une seule mention dans le texte : insuffisant
    assert t.proposer("note.docx", "voir aussi le vpn") == []
    # Dans le nom du fichier (poids 3) : suffisant, même avec des underscores
    assert t.proposer(r"Infra\Debug_VPN_v2.docx", "") == [Tag("logiciel", "VPN")]


def test_taxonomie_absente(tmp_path):
    assert Taxonomie.charger(tmp_path / "absente.toml").proposer("x", "y") == []


def test_taxonomie_du_projet_se_charge():
    t = Taxonomie.charger(RACINE / "taxonomie.toml")
    assert Tag("secteur", "Identité") in t.regles
    assert Tag("logiciel", "Impression") in t.proposer("", "Redémarrer le spouleur de l'imprimante. L'imprimante repart.")
