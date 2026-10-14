"""Captures d'écran de la GED pour le README, en thème sombre et clair.

Usage : uv run --with playwright python outils/demo/captures.py <url_du_serveur> <dossier_de_sortie>

Pilote le Chrome installé sur le poste (aucun navigateur à télécharger). À lancer sur un serveur
alimenté par les documents fictifs de generer_demo.py (voir outils/demo/README.md).
"""

import sys

from playwright.sync_api import sync_playwright

if len(sys.argv) != 3:
    raise SystemExit(__doc__)
BASE, SORTIE = sys.argv[1].rstrip("/"), sys.argv[2]

with sync_playwright() as p:
    navigateur = p.chromium.launch(channel="chrome")
    for theme, suffixe in (("dark", ""), ("light", "-clair")):
        page = navigateur.new_page(viewport={"width": 1280, "height": 860}, color_scheme=theme, locale="fr-FR")
        champ = page.locator("input[name=q]")

        def capturer(nom: str, pleine_page: bool = False) -> None:
            # Sans focus dans la barre : la liste de suggestions ne masque pas les résultats
            if champ.count():
                champ.blur()
            page.wait_for_timeout(400)
            page.screenshot(path=f"{SORTIE}/{nom}{suffixe}.png", full_page=pleine_page)

        page.goto(BASE + "/")
        capturer("accueil")

        page.goto(BASE + "/?q=vpn&vue=liste")
        capturer("recherche-liste")

        page.goto(BASE + "/")
        champ.fill("erreur")
        page.wait_for_timeout(800)
        page.keyboard.press("Escape")
        page.click("[data-deplier=tout]")
        capturer("recherche-arborescence")
        page.click("[data-deplier=dossiers]")  # remet la vue par défaut pour les captures suivantes

        page.goto(BASE + "/")
        champ.fill("ident")
        page.wait_for_timeout(800)
        page.screenshot(path=f"{SORTIE}/suggestions{suffixe}.png", clip={"x": 0, "y": 0, "width": 1280, "height": 420})

        page.goto(BASE + "/?q=vpn&vue=liste")
        champ.blur()  # sinon la liste de suggestions, ouverte au focus, couvre le résultat à cliquer
        page.get_by_role("link", name="Connexion VPN FortiClient impossible.docx").click()
        page.wait_for_load_state()
        capturer("document", pleine_page=True)
        page.close()
    navigateur.close()
print("Captures écrites dans", SORTIE)
