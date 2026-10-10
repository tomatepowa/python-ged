# GED DSI

> Moteur de recherche documentaire pour les partages réseau de la DSI : retrouver une note technique en quelques mots-clés, même à 3 h du matin en astreinte.

GED DSI indexe **en lecture seule** les dossiers documentaires existants (partages du NAS), en extrait le texte (Word, PDF, Excel, PowerPoint, mails, scans via OCR) et propose une interface web de recherche avec des filtres par pôle, type de document et tags (logiciel, secteur, type de note).

Les fichiers ne sont **ni déplacés, ni modifiés** : l'arborescence actuelle reste la référence, la GED se pose par-dessus.

---

## Sommaire

- [Fonctionnalités](#fonctionnalités)
- [Architecture](#architecture)
- [Prérequis](#prérequis)
- [Installation](#installation)
- [Configuration](#configuration)
- [Utilisation](#utilisation)
- [Mise en production](#mise-en-production)
- [Outil d'inventaire](#outil-dinventaire)
- [Structure du projet](#structure-du-projet)
- [Feuille de route](#feuille-de-route)
- [Développement](#développement)

---

## Fonctionnalités

- **Recherche plein texte en français** : insensible aux accents et aux pluriels (« procédures » trouve « procedure »), opérateurs de recherche web (`"phrase exacte"`, `-exclure`, `OR`).
- **Tolérance** : si rien n'est trouvé, recherche par début de mot (`imprim` → imprimante), puis par similarité sur les noms de fichiers (fautes de frappe).
- **Extraits surlignés** : chaque résultat montre le passage où apparaissent les mots cherchés.
- **Filtres** : pôle (Métier, Infra, BI, ITO…), type de fichier, tags.
- **Tags automatiques** par dictionnaire de mots-clés ([`taxonomie.toml`](taxonomie.toml)) : logiciel, secteur (identité, soins, facturation…), type de note.
- **Code documentaire** détecté dans l'en-tête ou le début du texte (format `DSI-INFRA-NT-001-v02`).
- **OCR** des PDF scannés et des images (captures d'écran, photos).
- **Doublons** : la fiche d'un document signale ses copies identiques ailleurs dans les partages.
- **Indexation incrémentale** : seuls les fichiers nouveaux ou modifiés sont relus ; les fichiers supprimés disparaissent des résultats.
- **Sécurité** : coffres KeePass, clés privées et certificats (`.kdbx`, `.pfx`, `.pem`…) ne sont jamais indexés ; les corbeilles et instantanés du NAS sont ignorés.

## Architecture

```
  Partages NAS (lecture seule)
  \\nas\metier\ged   \\nas\infra\ged   ...
            │
            ▼
  ┌───────────────────────┐   tâche planifiée (nuit)
  │  ged indexer          │   parcours → extraction texte → tags → base
  │  ged ocr              │   PDF scannés et images → texte
  └──────────┬────────────┘
             ▼
  ┌───────────────────────┐
  │  PostgreSQL           │   recherche plein texte (config. ged_fr :
  │                       │   français + sans accents), pg_trgm
  └──────────┬────────────┘
             ▼
  ┌───────────────────────┐
  │  ged serveur          │   FastAPI + Jinja2, http://serveur:8000
  └───────────────────────┘   accessible en VPN pour l'astreinte
```

| Composant | Choix | Pourquoi |
|---|---|---|
| Langage | Python 3.11+ | Meilleur écosystème pour l'extraction de documents et l'OCR |
| Base | PostgreSQL 16+ | Recherche plein texte française intégrée, pas de moteur supplémentaire à maintenir |
| PDF / OCR | PyMuPDF (Tesseract intégré) | Pas d'exécutable OCR à installer, seulement un fichier de langue |
| Web | FastAPI + Jinja2 | Pages rendues côté serveur, aucune dépendance JavaScript externe |
| Anciens formats | LibreOffice (optionnel) | Conversion `.doc` / `.xls` / `.ppt` |

Tout fonctionne nativement sous Windows Server, sans Docker.

## Prérequis

| Logiciel | Obligatoire | Remarque |
|---|---|---|
| [Python 3.11+](https://www.python.org/downloads/windows/) | oui | |
| [uv](https://docs.astral.sh/uv/) | recommandé | gestionnaire de projet Python, installable sans droits admin |
| [PostgreSQL 16+](https://www.postgresql.org/download/windows/) | oui | extensions `unaccent` et `pg_trgm` (incluses dans l'installeur standard) |
| Fichier de langue `fra.traineddata` | pour l'OCR | à récupérer sur [tesseract-ocr/tessdata_fast](https://github.com/tesseract-ocr/tessdata_fast) (ou `tessdata_best`, plus précis et plus lent) |
| [LibreOffice](https://fr.libreoffice.org/) | non | uniquement pour lire les anciens `.doc`, `.xls`, `.ppt` |

Le compte qui exécute l'indexation doit avoir un **accès en lecture** aux partages à indexer (idéalement un compte de service dédié, en lecture seule).

## Installation

```powershell
git clone <url-du-depot> ged-dsi
cd ged-dsi
uv sync
```

Créer la base (à adapter, en tant qu'administrateur PostgreSQL) :

```sql
CREATE ROLE ged LOGIN PASSWORD 'mot-de-passe-solide';
CREATE DATABASE ged OWNER ged ENCODING 'UTF8';
```

Puis initialiser le schéma :

```powershell
copy config.exemple.toml config.toml   # puis éditer config.toml
uv run ged init-db
```

> `init-db` crée les extensions `unaccent` et `pg_trgm`. Si le rôle `ged` n'a pas le droit de les créer, les créer une fois en superutilisateur dans la base `ged`.

## Configuration

Tout se règle dans `config.toml` (non versionné, voir [`config.exemple.toml`](config.exemple.toml)) :

```toml
[base]
url = "postgresql://ged:mot-de-passe@localhost:5432/ged"

[ocr]
active = true
tessdata = 'C:\GED\tessdata'     # dossier contenant fra.traineddata

[[sources]]
nom = "GED Infra"
chemin = '\\nas\infra\ged'
pole = "Infra"
```

Les tags automatiques se règlent dans [`taxonomie.toml`](taxonomie.toml) : un libellé, une liste de mots-clés. **La liste fournie est un premier jet à compléter avec les pôles** (logiciels métier réels, secteurs).

## Utilisation

```powershell
uv run ged indexer             # indexe les nouveautés (rapide si peu de changements)
uv run ged indexer --forcer    # relit tout (après modification de taxonomie.toml par exemple)
uv run ged ocr                 # passe en OCR les scans en attente
uv run ged ocr --limite 50     # ... par lots
uv run ged serveur             # interface web sur http://127.0.0.1:8000
```

Dans l'interface :

- taper quelques mots (`imprimante bloquée`, `"message d'erreur"`, `identitovigilance -formation`) ;
- affiner avec les filtres à gauche ;
- **Ouvrir** affiche le fichier (PDF, images) ou le télécharge (Word, Excel) ;
- **Copier le chemin** donne le chemin réseau à coller dans l'explorateur Windows ;
- la page **État de l'index** liste les sources, le volume indexé et les erreurs de lecture.

## Mise en production

Sur la VM Windows :

1. Installer Python, uv, PostgreSQL ; déposer `fra.traineddata` ; configurer `config.toml` avec `hote = "0.0.0.0"`.
2. **Indexation nocturne** : tâche planifiée Windows exécutée avec le compte de service :
   ```powershell
   uv run --directory C:\GED\ged-dsi ged indexer
   uv run --directory C:\GED\ged-dsi ged ocr
   ```
3. **Serveur web** permanent : service Windows (par exemple via [NSSM](https://nssm.cc/) ou [WinSW](https://github.com/winsw/winsw)) lançant `uv run ged serveur`.
4. Ouvrir le port dans le pare-feu Windows pour le réseau interne et le VPN.

> ⚠️ Version actuelle : **pas d'authentification**. À n'exposer que sur le réseau interne. L'authentification Active Directory est prévue (voir feuille de route).

## Outil d'inventaire

[`outils/inventaire/Inventaire-GED.ps1`](outils/inventaire/Inventaire-GED.ps1) : script PowerShell autonome (PowerShell 5.1+, sans droits admin, en lecture seule) qui dresse l'état des lieux d'une arborescence avant la mise en place de la GED : volumes par type, effort d'OCR, ancienneté, doublons, familles de versions, homogénéité de structure des `.docx`.

```powershell
powershell -ExecutionPolicy Bypass -File .\outils\inventaire\Inventaire-GED.ps1 `
    -Chemin '\\nas\metier\ged','\\nas\infra\ged' -Approfondi -Sortie C:\Temp\inventaire
```

Résultat : un `rapport.txt` de synthèse et des CSV détaillés (ouvrables dans Excel). Voir l'aide intégrée : `Get-Help .\outils\inventaire\Inventaire-GED.ps1 -Full`.

## Structure du projet

```
ged-dsi/
├── ged/
│   ├── __main__.py      # ligne de commande (init-db, indexer, ocr, serveur)
│   ├── config.py        # lecture de config.toml
│   ├── schema.sql       # schéma PostgreSQL (idempotent)
│   ├── db.py
│   ├── extraction.py    # texte et métadonnées par format
│   ├── ocr.py           # OCR via PyMuPDF / Tesseract
│   ├── taxonomie.py     # tags automatiques par mots-clés
│   ├── indexeur.py      # parcours des sources, indexation incrémentale
│   ├── recherche.py     # requêtes plein texte, filtres, extraits
│   ├── texte.py         # normalisation, détection du code documentaire
│   └── web/             # application FastAPI, gabarits, CSS
├── outils/inventaire/   # script PowerShell d'état des lieux
├── tests/
├── config.exemple.toml
├── taxonomie.toml
└── pyproject.toml
```

## Feuille de route

- [x] **Phase 0** — Inventaire des partages (script PowerShell)
- [x] **Phase 1** — Indexation en lecture seule, OCR, recherche web avec filtres et tags automatiques
- [ ] **Phase 2** — Dépôt de nouveaux documents : dossier « À classer », OCR, proposition de code et de tags, validation humaine, copie dans l'arborescence GED
- [ ] **Phase 3** — Conformité à la procédure des procédures : modèle `.dotx` à champs, génération des nouvelles notes, contrôle de structure, dates de révision
- [ ] **Phase 4** — Authentification Active Directory, tags manuels dans l'interface, liens avec la base de connaissances EasyVista, détection des versions, IA (selon la politique du groupe)

## Développement

```powershell
uv sync                 # installe aussi les dépendances de développement
uv run pytest           # tests (n'ont pas besoin de PostgreSQL)
```

Les tests génèrent leurs propres documents à la volée : aucun document réel n'est nécessaire ni ne doit être ajouté au dépôt.
