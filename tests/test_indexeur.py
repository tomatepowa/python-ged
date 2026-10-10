from pathlib import Path

from ged.config import charger
from ged.indexeur import parcourir
from ged.recherche import requete_prefixe, surligner


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


def test_requete_prefixe():
    assert requete_prefixe("imprim réseau") == "imprim:* & réseau:*"
    assert requete_prefixe("  !! ") is None


def test_surligner_echappe_le_html():
    resultat = surligner("<script>alert(1)</script> \x02imprimante\x03")
    assert "<script>" not in resultat
    assert "&lt;script&gt;" in resultat
    assert "<mark>imprimante</mark>" in resultat


def test_config_exemple_valide():
    racine = Path(__file__).resolve().parents[1]
    cfg = charger(racine / "config.exemple.toml")
    assert [s.pole for s in cfg.sources] == ["Métier", "Infra"]
    assert str(cfg.sources[0].chemin) == r"\\nas\metier\ged"
    assert cfg.taxonomie == racine / "taxonomie.toml"
