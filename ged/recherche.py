"""Requêtes de recherche plein texte."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from functools import partial
from itertools import islice, product
from urllib.parse import urlencode

import psycopg

from ged.acces import TOUT, FiltreAcces
from ged.arborescence import dossier_parent, fil_ariane
from ged.extraits import construire_motif, extraire
from ged.texte import normaliser

PAR_PAGE = 20
# Chemin affiché pour chaque résultat : les premiers dossiers (pôle, thème...) et le dossier
# final sont toujours visibles ; ceux du milieu se réduisent en « … » si la place manque
NIVEAUX_CHEMIN = 4
# Au-delà, la vue arborescence est tronquée (il faut affiner la recherche)
DOCUMENTS_ARBRE_MAX = 5000
# Bornes de la période (frise des dates) : "2020" ou "2020-03"
_PERIODE = re.compile(r"(\d{4})(?:-(\d{2}))?")
# Guillemets, exclusion (-mot) ou OR : syntaxe "moteur de recherche", traitée par websearch_to_tsquery
_OPERATEURS = re.compile(r'"|(?:^|\s)-\w|\sOR\s')


@dataclass
class Filtres:
    q: str = ""
    pole: str = ""
    famille: str = ""
    tags: list[int] = field(default_factory=list)
    # Limite la recherche à un dossier et ses sous-dossiers : "<id source>:<chemin relatif>"
    dossier: str = ""
    # Période de dernière modification, bornes incluses : "2020" ou "2020-03" (frise des dates)
    du: str = ""
    au: str = ""
    # Affichage des résultats : "arbre" (par défaut) ou "liste"
    vue: str = "arbre"
    # Ordre de la vue liste : "" (pertinence), "recent" ou "ancien"
    tri: str = ""
    page: int = 1

    def lien(self, page: int | None = None) -> str:
        """Query string de cette recherche (pour la pagination et l'URL partageable)."""
        parametres = [
            ("q", self.q), ("pole", self.pole), ("famille", self.famille), ("dossier", self.dossier),
            ("du", self.du), ("au", self.au), ("vue", "liste" if self.vue == "liste" else ""), ("tri", self.tri),
        ]
        parametres += [("tag", t) for t in self.tags]
        if (page or self.page) > 1:
            parametres.append(("page", page or self.page))
        return urlencode([(k, v) for k, v in parametres if v not in ("", None)])

    def dossier_cible(self) -> tuple[int, str] | None:
        """(id de la source, chemin relatif) du filtre dossier, None s'il est absent ou invalide."""
        source, _, chemin = self.dossier.partition(":")
        return (int(source), chemin.strip("\\")) if source.isdigit() else None

    def periode(self) -> tuple[date | None, date | None]:
        """(premier jour inclus, premier jour exclu) de la période ; None pour une borne absente ou invalide."""
        return _borne(self.du, fin=False), _borne(self.au, fin=True)


def _borne(valeur: str, fin: bool) -> date | None:
    """'2020' -> 1er janvier 2020 (ou 2021 pour une fin) ; '2020-03' -> 1er mars (ou 1er avril)."""
    m = _PERIODE.fullmatch(valeur.strip())
    if not m:
        return None
    annee, mois = int(m[1]), int(m[2] or 0)
    if not 1900 <= annee <= 2999 or mois > 12:
        return None
    if not mois:
        return date(annee + fin, 1, 1)
    if fin:
        annee, mois = (annee + 1, 1) if mois == 12 else (annee, mois + 1)
    return date(annee, mois, 1)


@dataclass
class Resultats:
    documents: list[dict]
    total: int
    # exact | saisie | corrige | approchant | recents
    mode: str
    # Requête réellement utilisée quand les mots tapés ont été corrigés
    correction: str | None = None
    # Pour chaque tag : nombre de documents trouvés qui le portent, c'est-à-dire le nombre de
    # résultats si on cochait ce tag en plus (les tags se combinent en ET)
    compte_tags: dict[int, int] = field(default_factory=dict)
    # Frise des dates : nombre de documents trouvés par mois de modification ("2020-03": 12),
    # sans tenir compte de la période choisie (la frise montre ce qu'il y a autour)
    compte_mois: dict[str, int] = field(default_factory=dict)


def decouper(q: str) -> list[str]:
    return re.findall(r"[^\W_]+", q)


def requete_saisie(mots: list[str]) -> str | None:
    """'imprimante bloq' -> 'imprimante & bloq:*' : le dernier mot est peut-être en cours de frappe."""
    if not mots:
        return None
    return " & ".join(mots[:-1] + [f"{mots[-1]}:*"])


CANDIDATS_PAR_MOT = 5
COMBINAISONS_MAX = 25


def corriger(conn: psycopg.Connection, mots: list[str], acces: FiltreAcces) -> list[str] | None:
    """Corrige les mots absents du corpus. None si rien à corriger.

    Un mot mal tapé a souvent plusieurs voisins aussi proches ("vante" : vente, santé, vant...).
    Parmi les combinaisons de candidats présentes dans au moins un document, on retient la plus
    proche de ce qui a été tapé (moins de lettres changées), puis, à égalité, celle que le plus
    de documents contiennent : "promesse vente" plutôt que "promesse santé".
    """
    candidats: list[list[tuple[str, int]]] = []
    for i, mot in enumerate(mots):
        n = normaliser(mot)
        if len(n) < 3 or n.isdigit():
            candidats.append([(mot, 0)])
            continue
        dernier = i == len(mots) - 1
        # Le dernier mot peut être un début de mot ("imprim") : il est valide s'il préfixe un mot connu
        connu = conn.execute(
            "SELECT 1 FROM vocabulaire WHERE mot LIKE %s LIMIT 1" if dernier
            else "SELECT 1 FROM vocabulaire WHERE mot = %s",
            (n.replace("%", "") + "%" if dernier else n,),
        ).fetchone()
        if connu:
            candidats.append([(mot, 0)])
            continue
        proches = conn.execute(
            # "%%" : opérateur de similarité pg_trgm "%" (doublé pour psycopg)
            "SELECT mot, levenshtein(mot, %(n)s) AS distance FROM vocabulaire "
            "WHERE mot %% %(n)s AND levenshtein(mot, %(n)s) <= %(max)s "
            "ORDER BY distance, nb_docs DESC LIMIT %(limite)s",
            {"n": n, "max": distance_max(n), "limite": CANDIDATS_PAR_MOT},
        ).fetchall()
        candidats.append([(p["mot"], p["distance"]) for p in proches] or [(mot, 0)])

    if all(len(c) == 1 and c[0][0] == m for c, m in zip(candidats, mots)):
        return None

    combinaisons = list(islice(product(*candidats), COMBINAISONS_MAX))
    nombres = compter(conn, [requete_saisie([mot for mot, _ in c]) for c in combinaisons], acces)
    meilleure, meilleur_score = None, None
    for combinaison, nb in zip(combinaisons, nombres):
        if not nb:
            continue
        # Moins de lettres changées d'abord, puis plus de documents
        score = (-sum(distance for _, distance in combinaison), nb)
        if meilleur_score is None or score > meilleur_score:
            meilleure, meilleur_score = [mot for mot, _ in combinaison], score
    return meilleure


def compter(conn: psycopg.Connection, requetes: list[str], acces: FiltreAcces) -> list[int]:
    """Nombre de documents visibles correspondant à chaque requête tsquery, en un seul aller-retour."""
    if not requetes:
        return []
    lignes = conn.execute(
        "SELECT (SELECT count(*) FROM document d WHERE d.supprime_le IS NULL "
        f"        AND d.recherche @@ to_tsquery('ged_fr', r.q) AND {acces.sql}) AS nb "
        "FROM unnest(%(requetes)s::text[]) WITH ORDINALITY AS r(q, rang) ORDER BY r.rang",
        {"requetes": requetes, **acces.parametres},
    ).fetchall()
    return [ligne["nb"] for ligne in lignes]


def distance_max(mot: str) -> int:
    """Nombre de lettres qu'une correction peut changer : 1 pour un mot court, 3 au plus."""
    return 1 if len(mot) <= 4 else 2 if len(mot) <= 8 else 3


def rechercher(conn: psycopg.Connection, filtres: Filtres, acces: FiltreAcces, tous: bool = False) -> Resultats:
    """Recherche paginée (tous=False) ou liste complète des documents trouvés, sans extraits,
    pour la vue arborescence (tous=True). Seuls les documents que `acces` laisse voir comptent."""
    executer = partial(_executer, conn, filtres, acces=acces, tous=tous)
    q = filtres.q.strip()
    if not q:
        return executer("recents")

    mots = decouper(q)
    if _OPERATEURS.search(q):
        resultats = executer("exact", parametre=q, surligner=mots)
        if resultats.total:
            return resultats
    else:
        # Fautes de frappe : on cherche d'abord la version corrigée, sinon des résultats
        # parasites sur les mots mal orthographiés masqueraient les bons documents
        correction = corriger(conn, mots, acces)
        if correction:
            resultats = executer("corrige", parametre=requete_saisie(correction), surligner=correction)
            if resultats.total:
                resultats.correction = " ".join(correction)
                return resultats
        if mots:
            resultats = executer("saisie", parametre=requete_saisie(mots), surligner=mots)
            if resultats.total:
                return resultats
    return executer("approchant", parametre=q, surligner=mots)


def _executer(
    conn: psycopg.Connection, filtres: Filtres, mode: str, parametre: str | None = None,
    surligner: list[str] | None = None, acces: FiltreAcces = TOUT, tous: bool = False,
) -> Resultats:
    parametres: dict = {
        "p": parametre,
        "limite": PAR_PAGE,
        "decalage": (max(filtres.page, 1) - 1) * PAR_PAGE,
        **acces.parametres,
    }
    conditions = ["d.supprime_le IS NULL", acces.sql]

    requete_ts = {
        "exact": "websearch_to_tsquery('ged_fr', %(p)s)",
        "saisie": "to_tsquery('ged_fr', %(p)s)",
        "corrige": "to_tsquery('ged_fr', %(p)s)",
    }.get(mode)

    if requete_ts:
        conditions.append(f"d.recherche @@ {requete_ts}")
        # Pertinence : normalisée par la longueur du document (option 1), plus un bonus
        # quand les mots sont dans le nom ou le titre (poids A)
        rang = (
            f"ts_rank_cd('{{0.1, 0.2, 0.4, 1.0}}', d.recherche, {requete_ts}, 1)"
            f" + CASE WHEN ts_filter(d.recherche, '{{a}}') @@ {requete_ts} THEN 1 ELSE 0 END"
        )
    elif mode == "approchant":
        # Tolérance aux fautes de frappe, sur le nom de fichier et le titre
        # ("<%%" est l'opérateur pg_trgm "<%", le % est doublé pour psycopg)
        conditions.append("(%(p)s <%% d.nom OR %(p)s <%% coalesce(d.titre, ''))")
        rang = "greatest(word_similarity(%(p)s, d.nom), word_similarity(%(p)s, coalesce(d.titre, '')))"
    else:  # recents
        rang = "0"

    if filtres.pole:
        conditions.append("s.pole = %(pole)s")
        parametres["pole"] = filtres.pole
    if filtres.famille:
        conditions.append("d.famille = %(famille)s")
        parametres["famille"] = filtres.famille
    if filtres.tags:
        # Le document doit porter TOUS les tags sélectionnés
        conditions.append(
            "d.id IN (SELECT document_id FROM document_tag WHERE tag_id = ANY(%(tags)s) "
            "GROUP BY document_id HAVING count(*) = %(nb_tags)s)"
        )
        parametres["tags"] = filtres.tags
        parametres["nb_tags"] = len(set(filtres.tags))
    if cible := filtres.dossier_cible():
        conditions.append("d.source_id = %(dossier_source)s AND starts_with(d.chemin_relatif, %(dossier_prefixe)s)")
        parametres["dossier_source"] = cible[0]
        parametres["dossier_prefixe"] = cible[1] + "\\" if cible[1] else ""

    compte_mois = {
        ligne["mois"]: ligne["nb"]
        for ligne in conn.execute(
            f"""
            SELECT to_char(d.modifie_le, 'YYYY-MM') AS mois, count(*) AS nb
            FROM document d JOIN source s ON s.id = d.source_id
            WHERE {' AND '.join(conditions)}
            GROUP BY 1
            """,
            parametres,
        )
    }
    debut, fin = filtres.periode()
    if debut:
        conditions.append("d.modifie_le >= %(debut)s")
        parametres["debut"] = debut
    if fin:
        conditions.append("d.modifie_le < %(fin)s")
        parametres["fin"] = fin

    compte_tags = {
        ligne["tag_id"]: ligne["nb"]
        for ligne in conn.execute(
            f"""
            SELECT dt.tag_id, count(*) AS nb
            FROM document d
            JOIN source s ON s.id = d.source_id
            JOIN document_tag dt ON dt.document_id = d.id
            WHERE {' AND '.join(conditions)}
            GROUP BY dt.tag_id
            """,
            parametres,
        )
    }

    if tous:
        # Vue arborescence : tous les documents trouvés, juste de quoi placer chacun dans l'arbre
        lignes = conn.execute(
            f"""
            SELECT d.id, d.source_id, s.nom AS source, d.nom, d.chemin_relatif, d.famille, d.modifie_le
            FROM document d JOIN source s ON s.id = d.source_id
            WHERE {' AND '.join(conditions)}
            ORDER BY s.nom, d.chemin_relatif
            LIMIT %(max_arbre)s
            """,
            {**parametres, "max_arbre": DOCUMENTS_ARBRE_MAX},
        ).fetchall()
        return Resultats(lignes, len(lignes), mode, compte_tags=compte_tags, compte_mois=compte_mois)

    # En deux temps : 1) classer tous les documents trouvés (rapide, index GIN), 2) ne lire le
    # texte et les tags que pour la page affichée. L'extrait surligné est fabriqué en Python
    # (ged.extraits) : ts_headline coûtait ~15 ms par long document.
    ordre = {"recent": "{t}modifie_le DESC", "ancien": "{t}modifie_le ASC"}.get(
        filtres.tri, "{t}rang DESC, {t}modifie_le DESC"
    )
    sql = f"""
        WITH page AS (
            SELECT d.id, {rang} AS rang, d.modifie_le, count(*) OVER () AS total
            FROM document d
            JOIN source s ON s.id = d.source_id
            WHERE {' AND '.join(conditions)}
            ORDER BY {ordre.format(t="")}, d.id
            LIMIT %(limite)s OFFSET %(decalage)s
        )
        SELECT d.id, d.source_id, d.nom, d.titre, d.chemin, d.chemin_relatif, d.famille, d.extension,
               d.statut, d.code_doc, d.modifie_le, d.taille, d.auteur, d.dernier_modificateur, s.pole,
               page.rang, page.total,
               left(coalesce(d.contenu, ''), 60000) AS contenu,
               coalesce((SELECT json_agg(json_build_object('id', t.id, 'categorie', t.categorie, 'libelle', t.libelle)
                                         ORDER BY t.categorie, t.libelle)
                         FROM document_tag dt JOIN tag t ON t.id = dt.tag_id
                         WHERE dt.document_id = d.id), '[]') AS tags
        FROM page
        JOIN document d ON d.id = page.id
        JOIN source s ON s.id = d.source_id
        ORDER BY {ordre.format(t="page.")}, page.id
    """
    lignes = conn.execute(sql, parametres).fetchall()
    motif = construire_motif(surligner or [])
    for ligne in lignes:
        ligne["extrait"] = extraire(ligne.pop("contenu"), motif)
        ligne["sous_titre"] = sous_titre(ligne["titre"], ligne["nom"])
        ligne["dossier"] = dossier_parent(ligne["chemin_relatif"])
        ligne["chemin_decoupe"] = decouper_chemin(fil_ariane(ligne["dossier"]))
    total = lignes[0]["total"] if lignes else 0
    return Resultats(lignes, total, mode, compte_tags=compte_tags, compte_mois=compte_mois)


COMPLETIONS_MAX = 6
DOCUMENTS_SUGGERES_MAX = 6


def suggerer(conn: psycopg.Connection, q: str, acces: FiltreAcces) -> dict:
    """Suggestions pendant la frappe, façon moteur de recherche : quelques millisecondes.

    - complétions : le dernier mot complété par les mots du corpus les plus fréquents
      ("imprim" -> "imprimante", "imprimer"), en ne gardant que celles qui donnent des résultats
      avec les mots déjà tapés ;
    - documents : ceux dont le nom ou le titre contient tous les mots tapés, ouvrables directement.
    """
    mots = [normaliser(m) for m in decouper(q)]
    if not mots:
        return {"completions": [], "documents": []}

    debut, dernier = mots[:-1], mots[-1]
    # Mots du corpus commençant par le dernier mot tapé, mots vides exclus ("votre", "des"...
    # n'ont pas de lexème dans la configuration ged_fr)
    candidats = [
        ligne["mot"] for ligne in conn.execute(
            "SELECT mot FROM vocabulaire WHERE mot LIKE %s AND to_tsvector('ged_fr', mot) <> ''::tsvector "
            "ORDER BY nb_docs DESC LIMIT %s",
            (dernier.replace("%", "").replace("_", "") + "%", COMPLETIONS_MAX * 4),
        )
    ]
    # Le vocabulaire vient de tous les documents : quand l'accès est restreint, on ne propose que
    # les mots présents dans des documents visibles (sinon un mot trahirait un document masqué)
    if (debut or acces is not TOUT) and candidats:
        # Plusieurs mots : on classe par nombre de documents contenant toute la combinaison
        # ("promesse v" -> "promesse vente" avant "promesse virement")
        nombres = compter(conn, [" & ".join(debut + [mot]) for mot in candidats], acces)
        candidats = [mot for mot, nb in sorted(zip(candidats, nombres), key=lambda c: -c[1]) if nb]
    completions = [" ".join(debut + [mot]) for mot in candidats[:COMPLETIONS_MAX]]

    documents = conn.execute(
        f"""
        SELECT d.id, d.nom, d.chemin_relatif, d.famille, s.pole
        FROM document d JOIN source s ON s.id = d.source_id
        WHERE d.supprime_le IS NULL AND {acces.sql}
          AND ged_normaliser(d.nom || ' ' || coalesce(d.titre, '')) LIKE ALL (%(motifs)s)
        ORDER BY similarity(ged_normaliser(d.nom), %(q)s) DESC, d.modifie_le DESC
        LIMIT %(limite)s
        """,
        {
            "motifs": ["%" + m.replace("%", "").replace("_", "") + "%" for m in mots],
            "q": " ".join(mots),
            "limite": DOCUMENTS_SUGGERES_MAX,
            **acces.parametres,
        },
    ).fetchall()
    for doc in documents:
        decoupe = decouper_chemin(fil_ariane(dossier_parent(doc.pop("chemin_relatif"))))
        doc["chemin"] = {partie: [nom for nom, _ in niveaux] for partie, niveaux in decoupe.items()}
    return {"completions": completions, "documents": documents}


def decouper_chemin(niveaux: list[tuple[str, str]]) -> dict[str, list[tuple[str, str]]]:
    """Sépare un chemin en début (toujours affiché), milieu (réductible) et dossier final.

    [a, b, c, d, e, f, g] -> début [a, b, c, d], milieu [e, f], fin [g]
    """
    if len(niveaux) <= NIVEAUX_CHEMIN + 1:
        return {"debut": niveaux, "milieu": [], "fin": []}
    return {"debut": niveaux[:NIVEAUX_CHEMIN], "milieu": niveaux[NIVEAUX_CHEMIN:-1], "fin": niveaux[-1:]}


def sous_titre(titre: str | None, nom: str) -> str | None:
    """Titre interne du document, s'il apporte une information que le nom de fichier n'a pas."""
    if not titre:
        return None
    t, n = normaliser(titre).strip(), normaliser(nom)
    if len(t) < 4 or t in n:
        return None
    return titre


def facettes(conn: psycopg.Connection, acces: FiltreAcces) -> dict:
    """Valeurs disponibles pour les filtres, avec le nombre de documents visibles."""
    actifs = f"d.supprime_le IS NULL AND {acces.sql}"
    return {
        "poles": conn.execute(
            f"SELECT s.pole AS valeur, count(*) AS nb FROM document d JOIN source s ON s.id = d.source_id "
            f"WHERE {actifs} GROUP BY s.pole ORDER BY s.pole",
            acces.parametres,
        ).fetchall(),
        "familles": conn.execute(
            f"SELECT famille AS valeur, count(*) AS nb FROM document d WHERE {actifs} "
            f"GROUP BY famille ORDER BY nb DESC",
            acces.parametres,
        ).fetchall(),
        "tags": conn.execute(
            f"SELECT t.id, t.categorie, t.libelle, count(*) AS nb FROM tag t "
            f"JOIN document_tag dt ON dt.tag_id = t.id JOIN document d ON d.id = dt.document_id "
            f"WHERE {actifs} GROUP BY t.id ORDER BY t.categorie, t.libelle",
            acces.parametres,
        ).fetchall(),
    }


def statistiques(conn: psycopg.Connection) -> dict:
    return {
        "par_statut": conn.execute(
            "SELECT statut, count(*) AS nb FROM document WHERE supprime_le IS NULL GROUP BY statut ORDER BY nb DESC"
        ).fetchall(),
        "par_source": conn.execute(
            "SELECT s.nom, s.pole, s.chemin, count(d.id) AS nb, max(d.indexe_le) AS derniere_indexation "
            "FROM source s LEFT JOIN document d ON d.source_id = s.id AND d.supprime_le IS NULL "
            "GROUP BY s.id ORDER BY s.nom"
        ).fetchall(),
        "erreurs": conn.execute(
            "SELECT id, chemin_relatif, erreur FROM document "
            "WHERE statut = 'erreur' AND supprime_le IS NULL ORDER BY chemin_relatif LIMIT 100"
        ).fetchall(),
    }
