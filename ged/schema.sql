-- Schéma de la GED. Idempotent : peut être rejoué sans risque (commande `ged init-db`).

CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS fuzzystrmatch;   -- levenshtein(), pour la correction orthographique

-- Configuration de recherche "français sans accents" :
-- "procédure", "Procedure" et "procédures" donnent le même lexème.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_ts_config WHERE cfgname = 'ged_fr') THEN
        CREATE TEXT SEARCH CONFIGURATION ged_fr (COPY = french);
        ALTER TEXT SEARCH CONFIGURATION ged_fr
            ALTER MAPPING FOR hword, hword_part, word WITH unaccent, french_stem;
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS source (
    id      serial PRIMARY KEY,
    nom     text NOT NULL UNIQUE,
    chemin  text NOT NULL,
    pole    text NOT NULL
);

CREATE TABLE IF NOT EXISTS document (
    id              bigserial PRIMARY KEY,
    source_id       int NOT NULL REFERENCES source (id),
    chemin          text NOT NULL UNIQUE,
    chemin_relatif  text NOT NULL,
    nom             text NOT NULL,
    extension       text NOT NULL,
    famille         text NOT NULL,
    taille          bigint NOT NULL,
    modifie_le      timestamptz NOT NULL,
    empreinte       text,
    -- indexe | ocr_requis | sans_texte (OCR sans texte lisible) | non_supporte | erreur
    statut          text NOT NULL,
    erreur          text,
    -- texte | conversion | ocr
    methode         text,
    titre           text,
    auteur          text,
    code_doc        text,
    nb_pages        int,
    contenu         text,
    vu_le           timestamptz NOT NULL DEFAULT now(),
    indexe_le       timestamptz,
    supprime_le     timestamptz,
    -- Vecteur de recherche pondéré : nom/titre (A) > code/chemin (B) > contenu (C).
    -- La ponctuation des noms de fichiers est remplacée par des espaces pour que
    -- "Debug_imprimantes.docx" soit trouvé par "imprimante".
    recherche tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('ged_fr', coalesce(titre, '') || ' ' || regexp_replace(nom, '[._\-]+', ' ', 'g')), 'A')
        || setweight(to_tsvector('ged_fr', coalesce(code_doc, '') || ' ' || regexp_replace(chemin_relatif, '[\\/._\-]+', ' ', 'g')), 'B')
        || setweight(to_tsvector('ged_fr', left(coalesce(contenu, ''), 500000)), 'C')
    ) STORED
);

CREATE INDEX IF NOT EXISTS document_recherche_idx ON document USING gin (recherche);
CREATE INDEX IF NOT EXISTS document_nom_trgm_idx ON document USING gin (nom gin_trgm_ops);
CREATE INDEX IF NOT EXISTS document_statut_idx ON document (statut) WHERE supprime_le IS NULL;
CREATE INDEX IF NOT EXISTS document_source_idx ON document (source_id);

CREATE TABLE IF NOT EXISTS tag (
    id          serial PRIMARY KEY,
    -- logiciel | secteur | type
    categorie   text NOT NULL,
    libelle     text NOT NULL,
    UNIQUE (categorie, libelle)
);

CREATE TABLE IF NOT EXISTS document_tag (
    document_id bigint NOT NULL REFERENCES document (id) ON DELETE CASCADE,
    tag_id      int NOT NULL REFERENCES tag (id) ON DELETE CASCADE,
    -- auto (proposé par la taxonomie) | manuel (posé par un utilisateur)
    origine     text NOT NULL DEFAULT 'auto',
    PRIMARY KEY (document_id, tag_id)
);

CREATE INDEX IF NOT EXISTS document_tag_tag_idx ON document_tag (tag_id);

-- Vocabulaire du corpus (mots sans accents, en minuscules), recalculé après chaque indexation.
-- Sert à corriger les fautes de frappe ("promese" -> "promesse") et à repérer le bruit de l'OCR.
CREATE TABLE IF NOT EXISTS vocabulaire (
    mot     text PRIMARY KEY,
    nb_docs int NOT NULL
);

CREATE INDEX IF NOT EXISTS vocabulaire_trgm_idx ON vocabulaire USING gin (mot gin_trgm_ops);
CREATE INDEX IF NOT EXISTS vocabulaire_prefixe_idx ON vocabulaire (mot text_pattern_ops);

-- Minuscules sans accents, déclarée IMMUTABLE pour pouvoir être indexée (unaccent ne l'est pas)
CREATE OR REPLACE FUNCTION ged_normaliser(texte text) RETURNS text
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$ SELECT lower(public.unaccent('public.unaccent'::regdictionary, texte)) $$;

-- Suggestions pendant la frappe : recherche dans les noms et titres de documents
CREATE INDEX IF NOT EXISTS document_nom_norm_trgm_idx
    ON document USING gin (ged_normaliser(nom || ' ' || coalesce(titre, '')) gin_trgm_ops);

-- Métadonnées de fichier (ajoutées après la première version : ALTER idempotents)
ALTER TABLE document ADD COLUMN IF NOT EXISTS cree_le timestamptz;
-- Dernier modificateur : propriétés Office (Windows ne le mémorise pas pour les autres fichiers)
ALTER TABLE document ADD COLUMN IF NOT EXISTS dernier_modificateur text;
-- Propriétaire NTFS du fichier
ALTER TABLE document ADD COLUMN IF NOT EXISTS proprietaire text;
-- Droits NTFS : [{"compte": "DOMAINE\groupe", "droits": "Lecture", "refus": false, "herite": true}, ...]
ALTER TABLE document ADD COLUMN IF NOT EXISTS droits jsonb;

-- Filtrage selon les droits NTFS de la personne connectée (voir ged/acces.py).
-- SID des comptes et groupes autorisés à lire le fichier (NULL : droits illisibles, document masqué)
ALTER TABLE document ADD COLUMN IF NOT EXISTS acces_sids text[];
-- Entrées de lecture dans l'ordre de la DACL, seulement s'il y a un refus : [["S-1-5-...", "A" | "R"], ...]
ALTER TABLE document ADD COLUMN IF NOT EXISTS acces_ordre jsonb;
CREATE INDEX IF NOT EXISTS document_acces_sids_idx ON document USING gin (acces_sids);
-- Droits du partage réseau de la source, même format que acces_ordre (NULL : non lus)
ALTER TABLE source ADD COLUMN IF NOT EXISTS acces_partage jsonb;

-- Comme Windows : la première entrée qui concerne la personne décide (refus ou autorisation).
-- `sids` : groupes actifs de la personne ; `sids_refus` : en plus, ses groupes « refus seulement ».
CREATE OR REPLACE FUNCTION ged_peut_lire(acl jsonb, sids text[], sids_refus text[]) RETURNS boolean
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$
    SELECT coalesce((
        SELECT e ->> 1 = 'A'
        FROM jsonb_array_elements(acl) WITH ORDINALITY AS a(e, rang)
        WHERE (e ->> 1 = 'A' AND e ->> 0 = ANY(sids)) OR (e ->> 1 = 'R' AND e ->> 0 = ANY(sids_refus))
        ORDER BY rang
        LIMIT 1
    ), false)
    $$;
