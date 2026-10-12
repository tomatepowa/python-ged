# GED DSI

> Moteur de recherche documentaire pour les partages réseau de la DSI : retrouver une note technique en quelques mots-clés, même à 3 h du matin en astreinte.

GED DSI indexe **en lecture seule** les dossiers documentaires existants (partages du NAS), en extrait le texte (Word, PDF, Excel, PowerPoint, mails, scans via OCR) et propose une interface web de recherche avec des filtres par pôle, type de document et tags (logiciel, secteur, type de note).

Les fichiers ne sont **ni déplacés, ni modifiés** : l'arborescence actuelle reste la référence, la GED se pose par-dessus.

---

## Aperçu

*Captures réalisées avec des documents fictifs ([`outils/demo`](outils/demo)).*

**Accueil : toute l'arborescence des partages, filtrable en temps réel**

<picture>
  <source media="(prefers-color-scheme: light)" srcset="docs/captures/accueil-clair.png">
  <img src="docs/captures/accueil.png" alt="Accueil en arborescence">
</picture>

**Recherche : les documents trouvés restent rangés dans leurs dossiers**

<picture>
  <source media="(prefers-color-scheme: light)" srcset="docs/captures/recherche-arborescence-clair.png">
  <img src="docs/captures/recherche-arborescence.png" alt="Résultats en arborescence">
</picture>

**Vue liste : extraits surlignés, code documentaire et tags**

<picture>
  <source media="(prefers-color-scheme: light)" srcset="docs/captures/recherche-liste-clair.png">
  <img src="docs/captures/recherche-liste.png" alt="Résultats en liste">
</picture>

**Suggestions pendant la frappe**

<picture>
  <source media="(prefers-color-scheme: light)" srcset="docs/captures/suggestions-clair.png">
  <img src="docs/captures/suggestions.png" alt="Suggestions">
</picture>

**Fiche d'un document : emplacement, personnes, droits NTFS, texte extrait**

<picture>
  <source media="(prefers-color-scheme: light)" srcset="docs/captures/document-clair.png">
  <img src="docs/captures/document.png" alt="Fiche document">
</picture>

---

## Sommaire

- [Aperçu](#aperçu)
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

- **Recherche instantanée** : les résultats se mettent à jour à chaque lettre, sans attente ; le dernier mot tapé est traité comme un début de mot (`imprim` → imprimante).
- **Suggestions façon moteur de recherche** : sous la barre, complétions tirées du vocabulaire des documents (« promesse v » → « promesse vente ») et documents dont le nom correspond, ouvrables directement ; navigation au clavier (↑ ↓ Entrée Échap).
- **Recherche plein texte en français** : insensible aux accents et aux pluriels (« procédures » trouve « procedure »), opérateurs de recherche web (`"phrase exacte"`, `-exclure`, `OR`).
- **Correction des fautes de frappe** à partir du vocabulaire réel des documents (« promese vante » → « promesse vente »), puis recherche approchante sur les noms de fichiers.
- **Classement** : un document dont le nom ou le titre contient les mots cherchés passe devant ceux qui les citent seulement dans le texte.
- **Extraits surlignés** : chaque résultat montre le passage où apparaissent les mots cherchés.
- **Filtres** : pôle (Métier, Infra, BI, ITO…), type de fichier, tags (un clic sur un tag affiche tous les documents qui le portent), dossier.
- **Frise des dates** sous la barre de recherche (bouton 📅, ouverte d'office quand une période est choisie) : courbe du nombre de documents trouvés par année de dernière modification (par mois sur 2 ans ou moins). Un clic sur une colonne filtre sur cette période, les deux poignées choisissent un intervalle. La date de création n'est pas utilisée : une copie ou une migration de NAS la remet à la date du transfert. La vue Liste se trie par pertinence, plus récents ou plus anciens d'abord.
- **Résultats en arborescence (vue par défaut)** : l'accueil range les documents trouvés dans leurs dossiers, avec le nombre de documents par dossier ; l'arbre suit la frappe et les filtres en temps réel. Sans recherche ni filtre, il montre toute la structure des partages. L'affichage dépend du contexte : à l'accueil, seuls les dossiers sont dépliés (« Dossiers seulement », un clic à côté du nom affiche les fichiers) ; un clic sur le nom d'un dossier y entre, comme dans l'explorateur (ses fichiers, ses sous-dossiers repliés, ⬆ pour remonter) ; une recherche déplie tout, fichiers compris. Les dossiers à un seul sous-dossier sont fusionnés (« Documents › Infra › Réseau »), chaque nom restant cliquable. Une vue Liste, avec extraits surlignés, reste disponible.
- **Chemins lisibles** : chaque résultat de la liste et des suggestions affiche son chemin en priorisant les 4 premiers dossiers (pôle, thème…) et le dossier final ; les dossiers intermédiaires se réduisent en « … » si la place manque ; chemin complet au survol.
- **Filtres actifs visibles** au-dessus des résultats, retirables ; compteurs des tags recalculés à chaque recherche, les tags menant à zéro résultat sont grisés.
- **Personnes** : auteur et dernier modificateur (propriétés Office / PDF), propriétaire du fichier.
- **Droits d'accès** : utilisateurs et groupes autorisés (ou refusés) sur chaque fichier, lus dans les droits NTFS et traduits comme dans l'onglet Sécurité (Lecture, Modification, Contrôle total…), avec leur origine (héritée ou propre au fichier). Relus à chaque indexation.
- **Tags automatiques** par dictionnaire de mots-clés ([`taxonomie.toml`](taxonomie.toml)) : logiciel, secteur (identité, soins, facturation…), type de note.
- **Code documentaire** détecté dans l'en-tête ou le début du texte (format `DSI-INFRA-NT-001-v02`).
- **OCR** des PDF scannés et des images (captures d'écran, photos). Les images sans texte lisible (photos, dessins, schémas) sont détectées et leur « texte » OCR, qui ne serait que du bruit, est écarté.
- **Doublons** : la fiche d'un document signale ses copies identiques ailleurs dans les partages.
- **Indexation incrémentale** : seuls les fichiers nouveaux ou modifiés sont relus ; les fichiers supprimés disparaissent des résultats. Un fichier renommé ou déplacé est reconnu à son empreinte : il garde sa fiche, son texte OCR et ses tags manuels.
- **Sécurité** : coffres KeePass, clés privées et certificats (`.kdbx`, `.pfx`, `.pem`…) ne sont jamais indexés ; les corbeilles et instantanés du NAS sont ignorés, tout comme les programmes et bibliothèques (`.exe`, `.dll`, `.jar`, `.msi`…). Les dossiers listés dans `exclure` (config) ne sont pas parcourus du tout : utile pour les dossiers d'applications.

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
| [PostgreSQL 16+](https://www.postgresql.org/download/windows/) | oui | extensions `unaccent`, `pg_trgm` et `fuzzystrmatch` (incluses dans l'installeur standard) |
| Fichier de langue `fra.traineddata` | pour l'OCR | à récupérer sur [tesseract-ocr/tessdata_fast](https://github.com/tesseract-ocr/tessdata_fast) (ou `tessdata_best`, plus précis et plus lent) |
| [LibreOffice](https://fr.libreoffice.org/) | non | uniquement pour lire les anciens `.doc`, `.xls`, `.ppt` |

Le compte qui exécute l'indexation doit avoir un **accès en lecture** aux partages à indexer (idéalement un compte de service dédié, en lecture seule). Pour afficher les droits d'accès, il doit aussi pouvoir lire les autorisations des fichiers (droit « Lire les autorisations », inclus dans la lecture standard) et joindre un contrôleur de domaine pour traduire les SID en noms de comptes.

> Les droits d'accès sont **affichés** à titre d'information : en l'absence d'authentification, la version actuelle ne filtre pas encore les résultats selon les droits de la personne connectée (prévu avec l'authentification Active Directory).

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

> `init-db` crée les extensions `unaccent`, `pg_trgm` et `fuzzystrmatch`. Si le rôle `ged` n'a pas le droit de les créer, les créer une fois en superutilisateur dans la base `ged`.

### Installation rapide sur un poste, sans droits administrateur

Pour tester sur un PC de travail : un script installe tout (uv, Python, PostgreSQL en version zip, fichier de langue OCR) dans `%LOCALAPPDATA%\ged`, sans toucher au système ni au PATH, crée la base et génère `config.toml` :

```powershell
powershell -ExecutionPolicy Bypass -File .\outils\installation\Installer-Poste.ps1 `
    -Source 'GED Infra|\\nas\infra\ged|Infra', 'GED Métier|\\nas\metier\ged|Métier'
```

Options : `-Port 5433` si un PostgreSQL utilise déjà le port 5432, `-Proxy http://proxy:8080` derrière un proxy, `-OcrPrecis` pour le modèle OCR le plus précis. Le script peut être relancé : les étapes déjà faites sont sautées.

PostgreSQL ne tourne pas en service Windows (il faudrait des droits admin). Au quotidien (précéder de `powershell -ExecutionPolicy Bypass -File` si l'exécution de scripts est bloquée sur le poste) :

```powershell
.\outils\installation\GED-Poste.ps1 demarrer    # PostgreSQL si besoin + interface sur http://127.0.0.1:8000
.\outils\installation\GED-Poste.ps1 indexer     # indexe les partages
.\outils\installation\GED-Poste.ps1 ocr         # passe les scans en OCR
.\outils\installation\GED-Poste.ps1 arreter     # arrête PostgreSQL
```

> Peuvent bloquer malgré l'absence de droits admin : AppLocker (exécutables lancés depuis le profil), un proxy, l'antivirus. Prévenir le service informatique avant un test sur un poste de production.

## Configuration

Tout se règle dans `config.toml` (non versionné, voir [`config.exemple.toml`](config.exemple.toml)) :

```toml
[base]
url = "postgresql://ged:mot-de-passe@localhost:5432/ged"

[ocr]
active = true
tessdata = 'C:\GED\tessdata'     # dossier contenant fra.traineddata

[interface]
exemples_recherche = "imprimante, identitovigilance, \"message d'erreur\""   # texte de la barre de recherche vide

[[sources]]
nom = "GED Infra"
chemin = '\\nas\infra\ged'
pole = "Infra"

[indexation]
exclure = [
    '\\nas\metier\ged\Applis\LogicielX',   # un dossier précis
    '*\Applis\*',                          # tous les dossiers « Applis », où qu'ils soient
]
```

**Exclure des dossiers** (dossiers d'applications, archives…) : chaque motif `exclure` porte sur le chemin complet, `*` remplaçant n'importe quelle suite de caractères, majuscules indifférentes. Un dossier visé n'est pas parcouru du tout ; au passage suivant de `ged indexer`, ses documents sortent de l'index (et y reviennent si le motif est retiré). Les programmes et bibliothèques (`.exe`, `.dll`, `.jar`, `.msi`…) sont déjà ignorés partout. Pour trouver le chemin d'un dossier : cliquer sur son nom dans l'arborescence, il s'affiche dans les filtres actifs.

Les tags automatiques se règlent dans [`taxonomie.toml`](taxonomie.toml) : un libellé, une liste de mots-clés (préfixe `titre:` pour un mot-clé qui ne compte que dans le nom ou le titre du document). Après modification, `ged recalculer` réapplique les règles sans relire les fichiers. **La liste fournie est un premier jet à compléter avec les pôles** (logiciels métier réels, secteurs).

## Utilisation

```powershell
uv run ged indexer             # indexe les nouveautés (rapide si peu de changements)
uv run ged indexer --forcer    # relit tous les fichiers (le texte OCR des fichiers inchangés est conservé)
uv run ged recalculer          # réapplique tags et codes sans relire les fichiers (après modif. de taxonomie.toml)
uv run ged ocr                 # passe en OCR les scans en attente
uv run ged ocr --limite 50     # ... par lots
uv run ged serveur             # interface web sur http://127.0.0.1:8000
```

Dans l'interface :

- taper quelques mots : les résultats s'affichent au fil de la frappe (`imprimante bloq`, `"message d'erreur"`, `identitovigilance -formation`) ;
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
│   ├── recherche.py     # requêtes plein texte, filtres, correction, suggestions
│   ├── extraits.py      # extraits surlignés des résultats
│   ├── arborescence.py  # arbre des résultats, outils sur les chemins
│   ├── droits.py        # propriétaire et droits NTFS (pywin32)
│   ├── texte.py         # normalisation, détection du code documentaire
│   └── web/             # application FastAPI, gabarits, CSS
├── outils/inventaire/   # script PowerShell d'état des lieux
├── outils/installation/ # installation sans droits admin et commandes du quotidien
├── outils/demo/         # documents fictifs et captures d'écran du README
├── docs/captures/       # captures d'écran (thèmes sombre et clair)
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
