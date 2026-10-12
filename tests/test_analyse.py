from ged.analyse import Document, mots, rapport
from ged.taxonomie import Regle, Tag, Taxonomie, _motif


def test_mots_utiles_des_noms_de_fichiers():
    assert mots("2021-03 Procédure_Imprimante (copie) v2.docx") == ["procedure", "imprimante"]
    assert mots("Main d'œuvre") == ["main", "d'oeuvre"]
    # Morceaux de GUID écartés, mais pas les sigles avec chiffres
    assert mots("{a92020-e3b1-b788} T2A W10") == ["t2a", "w10"]


def test_rapport_couverture_et_propositions():
    taxonomie = Taxonomie(regles={Tag("logiciel", "VPN"): Regle(_motif(["vpn"]), _motif(["vpn"]))})
    documents = [Document(rf"Infra\Citrix\{n}", n, None, 0) for n in ("Citrix StoreFront.docx", "Citrix panne.docx", "Citrix licence.docx")]
    documents += [Document(rf"Infra\Reseau\VPN {i}.docx", f"VPN {i}.docx", None, 1) for i in range(3)]
    texte = rapport(documents, taxonomie, {("logiciel", "VPN"): 3}, min_docs=3)
    assert "| logiciel | VPN | 3 |" in texte
    assert "| Citrix | 3 | 0 % |" in texte  # dossier de niveau 2 pas encore tagué
    assert '# "Citrix" = ["citrix"]    # 3 documents' in texte
    assert '"Vpn"' not in texte  # déjà couvert : pas proposé
