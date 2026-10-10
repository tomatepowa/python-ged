"""Requêtes de recherche plein texte."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import islice, product
from urllib.parse import urlencode

import psycopg

from ged.extraits import construire_motif, extraire
from ged.texte import normaliser

PAR_PAGE = 20
# Guillemets, exclusion (-mot) ou OR : syntaxe "moteur de recherche", traitée par websearch_to_tsquery
_OPERATEURS = re.compile(r'"|(?:^|\s)-\w|\sOR\s')


@dataclass
class Filtres:
    q: str = ""
    pole: str = ""
    famille: str = ""
    tags: list[int] = field(default_factory=list)
    page: int = 1

    def lien(self, page: int | None = None) -> str:
        """Query string de cette recherche (pour la pagination et l'URL partageable)."""
        parametres = [("q", self.q), ("pole", self.pole), ("famille", self.famille)]
        parametres += [("tag", t) for t in self.tags]
        parametres.append(("page", page or self.page))
        return urlencode([(k, v) for k, v in parametres if v not in ("", None)])


@dataclass
class Resultats:
    documents: list[dict]
    total: int
    # exact | saisie | corrige | approchant | recents
    mode: str
    # Requête réellement utilisée quand les mots tapés ont été corrigés
    correction: str | None = None


def decouper(q: str) -> list[str]:
    return re.findall(r"[^\W_]+", q)


def requete_saisie(mots: list[str]) -> str | None:
    """'imprimante bloq' -> 'imprimante & bloq:*' : le dernier mot est peut-être en cours de frappe."""
    if not mots:
        return None
    return " & ".join(mots[:-1] + [f"{mots[-1]}:*"])


CANDIDATS_PAR_MOT = 5
COMBINAISONS_MAX = 25


def corriger(conn: psycopg.Connection, mots: list[str]) -> list[str] | None:
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
    nombres = compter(conn, [requete_saisie([mot for mot, _ in c]) for c in combinaisons])
    meilleure, meilleur_score = None, None
    for combinaison, nb in zip(combinaisons, nombres):
        if not nb:
            continue
        # Moins de lettres changées d'abord, puis plus de documents
        score = (-sum(distance for _, distance in combinaison), nb)
        if meilleur_score is None or score > meilleur_score:
            meilleure, meilleur_score = [mot for mot, _ in combinaison], score
    return meilleure


def compter(conn: psycopg.Connection, requetes: list[str]) -> list[int]:
    """Nombre de documents correspondant à chaque requête tsquery, en un seul aller-retour."""
    if not requetes:
        return []
    lignes = conn.execute(
        "SELECT (SELECT count(*) FROM document d WHERE d.supprime_le IS NULL "
        "        AND d.recherche @@ to_tsquery('ged_fr', r.q)) AS nb "
        "FROM unnest(%s::text[]) WITH ORDINALITY AS r(q, rang) ORDER BY r.rang",
        (requetes,),
    ).fetchall()
    return [ligne["nb"] for ligne in lignes]


def distance_max(mot: str) -> int:
    """Nombre de lettres qu'une correction peut changer : 1 pour un mot court, 3 au plus."""
    return 1 if len(mot) <= 4 else 2 if len(mot) <= 8 else 3


def rechercher(conn: psycopg.Connection, filtres: Filtres) -> Resultats:
    q = filtres.q.strip()
    if not q:
        return _executer(conn, filtres, "recents")

    mots = decouper(q)
    if _OPERATEURS.search(q):
        resultats = _executer(conn, filtres, "exact", parametre=q, surligner=mots)
        if resultats.total:
            return resultats
    else:
        # Fautes de frappe : on cherche d'abord la version corrigée, sinon des résultats
        # parasites sur les mots mal orthographiés masqueraient les bons documents
        correction = corriger(conn, mots)
        if correction:
            resultats = _executer(conn, filtres, "corrige", parametre=requete_saisie(correction), surligner=correction)
            if resultats.total:
                resultats.correction = " ".join(correction)
                return resultats
        if mots:
            resultats = _executer(conn, filtres, "saisie", parametre=requete_saisie(mots), surligner=mots)
            if resultats.total:
                return resultats
    return _executer(conn, filtres, "approchant", parametre=q, surligner=mots)


def _executer(
    conn: psycopg.Connection, filtres: Filtres, mode: str, parametre: str | None = None,
    surligner: list[str] | None = None,
) -> Resultats:
    parametres: dict = {
        "p": parametre,
        "limite": PAR_PAGE,
        "decalage": (max(filtres.page, 1) - 1) * PAR_PAGE,
    }
    conditions = ["d.supprime_le IS NULL"]

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

    # En deux temps : 1) classer tous les documents trouvés (rapide, index GIN), 2) ne lire le
    # texte et les tags que pour la page affichée. L'extrait surligné est fabriqué en Python
    # (ged.extraits) : ts_headline coûtait ~15 ms par long document.
    sql = f"""
        WITH page AS (
            SELECT d.id, {rang} AS rang, d.modifie_le, count(*) OVER () AS total
            FROM document d
            JOIN source s ON s.id = d.source_id
            WHERE {' AND '.join(conditions)}
            ORDER BY rang DESC, d.modifie_le DESC
            LIMIT %(limite)s OFFSET %(decalage)s
        )
        SELECT d.id, d.nom, d.titre, d.chemin, d.chemin_relatif, d.famille, d.extension, d.statut,
               d.code_doc, d.modifie_le, d.taille, s.pole, page.rang, page.total,
               left(coalesce(d.contenu, ''), 60000) AS contenu,
               coalesce((SELECT json_agg(json_build_object('categorie', t.categorie, 'libelle', t.libelle)
                                         ORDER BY t.categorie, t.libelle)
                         FROM document_tag dt JOIN tag t ON t.id = dt.tag_id
                         WHERE dt.document_id = d.id), '[]') AS tags
        FROM page
        JOIN document d ON d.id = page.id
        JOIN source s ON s.id = d.source_id
        ORDER BY page.rang DESC, page.modifie_le DESC
    """
    lignes = conn.execute(sql, parametres).fetchall()
    motif = construire_motif(surligner or [])
    for ligne in lignes:
        ligne["extrait"] = extraire(ligne.pop("contenu"), motif)
        ligne["sous_titre"] = sous_titre(ligne["titre"], ligne["nom"])
    total = lignes[0]["total"] if lignes else 0
    return Resultats(lignes, total, mode)


COMPLETIONS_MAX = 6
DOCUMENTS_SUGGERES_MAX = 6


def suggerer(conn: psycopg.Connection, q: str) -> dict:
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
    if debut and candidats:
        # Plusieurs mots : on classe par nombre de documents contenant toute la combinaison
        # ("promesse v" -> "promesse vente" avant "promesse virement")
        nombres = compter(conn, [" & ".join(debut + [mot]) for mot in candidats])
        candidats = [mot for mot, nb in sorted(zip(candidats, nombres), key=lambda c: -c[1]) if nb]
    completions = [" ".join(debut + [mot]) for mot in candidats[:COMPLETIONS_MAX]]

    documents = conn.execute(
        """
        SELECT d.id, d.nom, d.chemin_relatif, d.famille, s.pole
        FROM document d JOIN source s ON s.id = d.source_id
        WHERE d.supprime_le IS NULL
          AND ged_normaliser(d.nom || ' ' || coalesce(d.titre, '')) LIKE ALL (%(motifs)s)
        ORDER BY similarity(ged_normaliser(d.nom), %(q)s) DESC, d.modifie_le DESC
        LIMIT %(limite)s
        """,
        {
            "motifs": ["%" + m.replace("%", "").replace("_", "") + "%" for m in mots],
            "q": " ".join(mots),
            "limite": DOCUMENTS_SUGGERES_MAX,
        },
    ).fetchall()
    return {"completions": completions, "documents": documents}


def sous_titre(titre: str | None, nom: str) -> str | None:
    """Titre interne du document, s'il apporte une information que le nom de fichier n'a pas."""
    if not titre:
        return None
    t, n = normaliser(titre).strip(), normaliser(nom)
    if len(t) < 4 or t in n:
        return None
    return titre


def facettes(conn: psycopg.Connection) -> dict:
    """Valeurs disponibles pour les filtres, avec le nombre de documents."""
    actifs = "d.supprime_le IS NULL"
    return {
        "poles": conn.execute(
            f"SELECT s.pole AS valeur, count(*) AS nb FROM document d JOIN source s ON s.id = d.source_id "
            f"WHERE {actifs} GROUP BY s.pole ORDER BY s.pole"
        ).fetchall(),
        "familles": conn.execute(
            f"SELECT famille AS valeur, count(*) AS nb FROM document d WHERE {actifs} "
            f"GROUP BY famille ORDER BY nb DESC"
        ).fetchall(),
        "tags": conn.execute(
            f"SELECT t.id, t.categorie, t.libelle, count(*) AS nb FROM tag t "
            f"JOIN document_tag dt ON dt.tag_id = t.id JOIN document d ON d.id = dt.document_id "
            f"WHERE {actifs} GROUP BY t.id ORDER BY t.categorie, t.libelle"
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
