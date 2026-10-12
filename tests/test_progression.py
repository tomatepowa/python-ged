from ged.progression import Progression, nombre

CHEMIN = r"Documents\Famille\Banque\Relevés 2019-2020\2020-03 Relevé de compte courant.pdf"


def progression_a_mi_course() -> Progression:
    p = Progression(2741, "Indexation")
    p.faits = 1234
    p.debut -= 380  # 380 s écoulées
    p.en_cours = CHEMIN
    return p


def test_ligne_large_complete():
    p = progression_a_mi_course()
    ligne = p.texte(p.debut + 380, largeur=200)
    assert "45%" in ligne and "1 234/2 741" in ligne and "reste ~8 min" in ligne
    assert ligne.endswith(CHEMIN)


def test_console_etroite_garde_le_nom_du_fichier():
    p = progression_a_mi_course()
    ligne = p.texte(p.debut + 380, largeur=80)
    assert len(ligne) <= 80
    assert ligne.endswith("courant.pdf") and "…" in ligne


def test_pas_d_estimation_les_premieres_secondes():
    p = progression_a_mi_course()
    assert "reste" not in p.texte(p.debut + 1, largeur=200)


def test_nombre_sans_espace_insecable():
    # Une sortie redirigée en cp1252 ne saurait pas écrire l'espace fine insécable
    assert nombre(1234567) == "1 234 567"
    nombre(1234567).encode("cp1252")


def test_page_de_l_ocr_toujours_visible():
    p = progression_a_mi_course()
    p.page(12, 40)
    for largeur in (200, 80):
        assert p.texte(p.debut + 380, largeur=largeur).endswith("courant.pdf (p. 12/40)")
    p.fichier("autre.pdf")
    assert "p. 12" not in p.texte(p.debut + 380, largeur=200)
