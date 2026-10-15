# GED DSI

> Moteur de recherche documentaire pour les partages réseau de la DSI : retrouver une note technique en quelques mots-clés, même à 3 h du matin en astreinte.

GED DSI indexe **en lecture seule** les dossiers documentaires existants (partages du NAS), en extrait le texte (Word, PDF, Excel, PowerPoint, mails, scans via OCR) et propose une recherche web avec filtres par pôle, type de document, tags, date et personne. Chacun n'y voit que les documents que ses droits Windows lui permettent d'ouvrir.

Les fichiers ne sont **ni déplacés, ni modifiés** : l'arborescence actuelle reste la référence, la GED se pose par-dessus.

## Aperçu

*Captures réalisées avec des documents fictifs ([`outils/demo`](outils/demo)).*

**Accueil : toute l'arborescence des partages, filtrable en temps réel**

<picture>
  <source media="(prefers-color-scheme: light)" srcset="docs/captures/accueil-clair.png">
  <img src="docs/captures/accueil.png" alt="Accueil en arborescence">
</picture>

**Vue liste : extraits surlignés, code documentaire, tags, aperçu des dates**

<picture>
  <source media="(prefers-color-scheme: light)" srcset="docs/captures/recherche-liste-clair.png">
  <img src="docs/captures/recherche-liste.png" alt="Résultats en liste">
</picture>

<details>
<summary><b>Plus de captures</b> : recherche en arborescence, suggestions, fiche d'un document</summary>

<picture>
  <source media="(prefers-color-scheme: light)" srcset="docs/captures/recherche-arborescence-clair.png">
  <img src="docs/captures/recherche-arborescence.png" alt="Résultats en arborescence">
</picture>

<picture>
  <source media="(prefers-color-scheme: light)" srcset="docs/captures/suggestions-clair.png">
  <img src="docs/captures/suggestions.png" alt="Suggestions">
</picture>

<picture>
  <source media="(prefers-color-scheme: light)" srcset="docs/captures/document-clair.png">
  <img src="docs/captures/document.png" alt="Fiche document">
</picture>

</details>

## Fonctionnalités

**Recherche**
- **Instantanée** : résultats à chaque lettre, le dernier mot est un début de mot (`imprim` → imprimante).
- **Suggestions** sous la barre : complétions tirées du vocabulaire des documents et documents dont le nom correspond ; clavier ↑ ↓ Entrée Échap.
- **Plein texte en français**, insensible aux accents et aux pluriels ; syntaxe web (`"phrase exacte"`, `-exclure`, `OR`).
- **Fautes de frappe corrigées** d'après le vocabulaire réel (« promese vante » → « promesse vente »), puis recherche approchante sur les noms de fichiers.
- **Classement** : les mots présents dans le nom ou le titre comptent plus que dans le texte ; extrait surligné pour chaque résultat.

**Navigation et filtres**
- **Arborescence** (vue par défaut) : les documents trouvés restent rangés dans leurs dossiers, avec leur nombre ; un clic sur un dossier y entre comme dans l'explorateur. Vue **liste** au choix, triée par pertinence ou par date.
- **Filtres** : pôle, type de fichier, tags, dossier, **période** (aperçu de la courbe des dates dans la barre de recherche, frise complète d'un clic) et **personne** (un clic sur un auteur, un dernier modificateur ou un propriétaire). Filtres actifs rappelés au-dessus des résultats, compteurs recalculés à chaque recherche.
- **Chemins lisibles** : début et dossier final toujours visibles, milieu réduit en « … » si la place manque.

**Documents**
- **Fiche** : emplacement, code documentaire, tags, auteur et dernier modificateur (propriétés Office / PDF), propriétaire, droits NTFS traduits comme dans l'onglet Sécurité, copies identiques ailleurs, texte extrait.
- **Tags automatiques** par mots-clés ([`taxonomie.toml`](taxonomie.toml)) : logiciel, secteur, type de note.
- **Code documentaire** détecté dans le nom du fichier, sinon en tête du texte (`DSI-INFRA-NT-001-v02`, `NT.OC.GSI.01`, `NT_OC_GSI_01`).
- **OCR** des PDF scannés et des images ; le « texte » des photos et schémas sans écriture est écarté.

**Accès**
- **Identification par la session Windows**, sans mot de passe à saisir, et **filtrage selon les droits NTFS** partout : résultats, suggestions, compteurs, fiches, fichiers. Voir [Droits d'accès et identification](#droits-daccès-et-identification).
- **Page « Mes groupes »** (clic sur son nom) : ses documents (créés, modifiés, possédés) et ses groupes Windows, en arbre quand l'Active Directory répond. La fiche d'un document dit quels groupes en donnent l'accès.

**Indexation**
- **Incrémentale** : seuls les fichiers nouveaux ou modifiés sont relus, les droits sont relus à chaque passage. Un fichier renommé ou déplacé est reconnu à son empreinte et garde sa fiche, son OCR et ses tags.
- **Jamais indexés** : coffres KeePass, clés et certificats (`.kdbx`, `.pfx`, `.pem`…), programmes (`.exe`, `.dll`, `.msi`…), corbeilles et instantanés du NAS, dossiers listés dans `exclure`.

## Architecture

```
  Partages NAS (lecture seule)
  \\nas\metier\ged   \\nas\infra\ged   ...
            │
            ▼
  ┌───────────────────────┐   tâche planifiée (nuit)
  │  ged indexer          │   parcours → texte → tags → droits NTFS → base
  │  ged ocr              │   PDF scannés et images → texte
  └──────────┬────────────┘
             ▼
  ┌───────────────────────┐
  │  PostgreSQL           │   recherche plein texte (français, sans accents)
  └──────────┬────────────┘
             ▼
  ┌───────────────────────┐   identification Windows (NTLM / Kerberos),
  │  ged serveur          │   filtrage selon les droits de la personne
  └───────────────────────┘   http://serveur:8000, accessible en VPN
```

| Composant | Choix | Pourquoi |
|---|---|---|
| Langage | Python 3.11+ | Meilleur écosystème pour l'extraction de documents et l'OCR |
| Base | PostgreSQL 16+ | Recherche plein texte française intégrée, pas de moteur à maintenir à côté |
| PDF / OCR | PyMuPDF (Tesseract intégré) | Pas d'exécutable OCR à installer, seulement un fichier de langue |
| Web | FastAPI + Jinja2 | Pages rendues côté serveur, aucune dépendance JavaScript externe |
| Windows | pywin32 | Droits NTFS, identification SSPI, annuaire Active Directory |

**Pourquoi pas Docker ?** La GED tourne nativement sur une VM Windows, et une bonne part de son travail en dépend : lire les partages avec un compte du domaine, lire les droits NTFS, traduire les SID en noms, identifier les personnes par NTLM / Kerberos (SSPI), lire les groupes dans l'Active Directory. Dans un conteneur Linux, tout cela serait à réécrire (montage SMB avec identifiants stockés, Samba, LDAP, keytab Kerberos créée par un admin AD) ; un conteneur Windows ne rejoint pas le domaine sans gMSA (admin AD) et demande un hôte Windows Server. Installer Docker exige en plus des droits admin, et Docker Desktop est payant au-delà de 250 salariés. `uv` et le script d'installation donnent déjà un déploiement reproductible sans droits admin (versions figées par `uv.lock`). Docker reste pratique pour un **PostgreSQL de développement ou de test**.

## Installation

| Logiciel | Obligatoire | Remarque |
|---|---|---|
| [Python 3.11+](https://www.python.org/downloads/windows/) et [uv](https://docs.astral.sh/uv/) | oui | uv s'installe sans droits admin |
| [PostgreSQL 16+](https://www.postgresql.org/download/windows/) | oui | extensions `unaccent`, `pg_trgm`, `fuzzystrmatch` (incluses dans l'installeur) |
| `fra.traineddata` | pour l'OCR | [tessdata_fast](https://github.com/tesseract-ocr/tessdata_fast) (ou `tessdata_best`, plus précis et plus lent) |
| [LibreOffice](https://fr.libreoffice.org/) | non | pour les anciens `.doc`, `.xls`, `.ppt` |

Le compte qui indexe doit pouvoir **lire** les partages (idéalement un compte de service en lecture seule) et joindre un contrôleur de domaine pour traduire les SID en noms de comptes.

### Poste de test, sans droits administrateur

Un script installe tout (uv, Python, PostgreSQL en version zip, fichier de langue OCR) dans `%LOCALAPPDATA%\ged`, sans toucher au système ni au PATH, crée la base et génère `config.toml`. Il peut être relancé : les étapes faites sont sautées.

```powershell
powershell -ExecutionPolicy Bypass -File .\outils\installation\Installer-Poste.ps1 `
    -Source 'GED Infra|\\nas\infra\ged|Infra', 'GED Métier|\\nas\metier\ged|Métier'
```

Options : `-Port 5433` (si le port 5432 est pris), `-Proxy http://proxy:8080`, `-OcrPrecis`. Au quotidien, `.\outils\installation\GED-Poste.ps1 <commande>` démarre PostgreSQL si besoin puis lance la commande : `demarrer` (interface sur http://localhost:8000), `preparer`, `verifier`, `indexer`, `ocr`, `analyser`, `reinitialiser`, `arreter`.

> Peuvent bloquer malgré l'absence de droits admin : AppLocker, un proxy, l'antivirus. Prévenir le service informatique avant un test sur un poste de production.

### Serveur (VM Windows)

```powershell
git clone <url-du-depot> C:\GED\ged-dsi
cd C:\GED\ged-dsi
uv sync
copy config.exemple.toml config.toml   # puis l'adapter, avec hote = "0.0.0.0"
```

```sql
-- en administrateur PostgreSQL
CREATE ROLE ged LOGIN PASSWORD 'mot-de-passe-solide';
CREATE DATABASE ged OWNER ged ENCODING 'UTF8';
```

`ged indexer` et `ged serveur` créent ou mettent à jour le schéma à leur démarrage (`ged init-db` le fait seul). Si le rôle `ged` ne peut pas créer les extensions, les créer une fois en superutilisateur.

1. **Indexation nocturne** : tâche planifiée exécutée avec le compte de service : `uv run --directory C:\GED\ged-dsi ged indexer`, puis `... ged ocr`.
2. **Serveur web** permanent : service Windows ([NSSM](https://nssm.cc/), [WinSW](https://github.com/winsw/winsw)) lançant `uv run ged serveur`.
3. **Pare-feu** : ouvrir le port pour le réseau interne et le VPN.
4. **Identification** : renseigner `groupes_admin` et vérifier la zone « Intranet local » sur les postes (voir ci-dessous).

> ⚠️ L'interface est servie en HTTP : le cookie de session et les documents circulent en clair. À réserver au réseau interne et au VPN ; pour aller plus loin, placer un proxy HTTPS devant.

## Configuration

Tout se règle dans `config.toml` (non versionné, modèle commenté : [`config.exemple.toml`](config.exemple.toml)) :

```toml
[base]
url = "postgresql://ged:mot-de-passe@localhost:5432/ged"

[ocr]
active = true
tessdata = 'C:\GED\tessdata'     # dossier contenant fra.traineddata

[indexation]
exclure = ['*\Applis\*']         # dossiers non parcourus (chemin complet, * joker, casse ignorée)

[authentification]
groupes_admin = ['CLINIQUES\GED-Admins']   # voient tous les documents et l'état de l'index

[[sources]]                      # une section par dossier à indexer
nom = "GED Infra"
chemin = '\\nas\infra\ged'
pole = "Infra"
```

### Première mise en route

1. **`ged preparer`** parcourt les sources sans ouvrir aucun fichier (noms, tailles, dates : quelques minutes) et ouvre une page sur http://127.0.0.1:8001 avec l'arborescence. Chaque dossier affiche son nombre de fichiers et de sous-dossiers, coloré selon son poids dans la source, son volume et l'année du fichier le plus récent. Des indices signalent les dossiers à vérifier (nom technique, programmes, sauvegardes, rien de modifié depuis 5 ans, dossier illisible) sans rien décocher d'office. Tout est coché : on décoche ce qu'on ne veut pas indexer, on valide, et les choix sont écrits dans `exclusions.toml` (non versionné, lu en plus de `exclure`). La commande peut être relancée : les choix précédents sont repris.
2. **`ged verifier`** contrôle la base, l'accès aux sources (et à leurs droits de partage), les exclusions, la taxonomie, l'OCR, LibreOffice, le port de l'interface et les groupes d'administration. Il mesure aussi, avec le compte qui lance la GED, la remontée des groupes (noms, hiérarchie dans l'Active Directory, durée), et lit propriétaire et droits d'un échantillon de fichiers par source (200 au plus, 5 par dossier, sans les ouvrir) : propriétaires principaux, fichiers aux droits illisibles (ils seraient masqués), temps de lecture par fichier. Code retour 1 en cas d'erreur.
3. **`ged indexer`** lance l'indexation complète.

Un dossier exclu sort de l'index au passage suivant de `ged indexer` (et y revient si le motif est retiré) ; son chemin s'affiche dans les filtres actifs quand on clique dessus dans l'arborescence.

**Tags** : [`taxonomie.toml`](taxonomie.toml) associe à chaque libellé des mots-clés (préfixe `titre:` pour un mot qui ne compte que dans le nom ou le titre). `ged recalculer` réapplique les règles sans relire les fichiers ; `ged analyser` produit un rapport (dossiers, mots fréquents des noms, couverture des tags) pour la construire, et [`CLAUDE.md`](CLAUDE.md) décrit la marche à suivre avec Claude Code, si la politique du groupe l'autorise. La liste fournie est un premier jet à compléter avec les pôles.

**Tags posés à la main** (administrateurs de la GED). Sur la fiche d'un document : **×** retire un tag (la taxonomie ne le remettra plus), le champ **Ajouter** pose un tag existant ou en crée un (catégorie et libellé ; « veeam » retrouve « Veeam »). Après un clic sur un dossier, **Poser sur le dossier** donne le tag à tous ses documents, sous-dossiers compris, y compris ceux qui y arriveront. Ces choix sont gardés en base, respectés par `ged indexer` et `ged recalculer`, et perdus par `ged reinitialiser` (qui le signale). Un tag créé ainsi ne vit qu'en base : pour qu'il soit aussi posé automatiquement, l'ajouter à `taxonomie.toml` avec ses mots-clés.

## Droits d'accès et identification

```toml
[authentification]
active = true                 # false : pas d'identification, tout le monde voit tout (démo)
protocole = "NTLM"            # ou "Negotiate" (Kerberos)
groupes_admin = ['CLINIQUES\GED-Admins']
duree_session_heures = 8      # un changement de groupe compte à la session suivante
```

**Identification.** Le serveur demande au navigateur de s'identifier ; Windows vérifie l'identité auprès du contrôleur de domaine et fournit tous les groupes de la personne, imbriqués compris. Un cookie de session (jeton aléatoire, sans nom ni groupe) prend le relais.
- **Edge et Chrome** envoient la session Windows sans rien demander pour les sites de la zone « Intranet local » : c'est le cas d'un nom court (`http://vm-ged:8000`, `http://localhost:8000`). Pour une adresse IP ou un nom complet, l'ajouter à la zone par GPO (*Liste des attributions de sites aux zones*) ; `127.0.0.1` est redirigé vers `localhost`.
- **Firefox** ne le fait que pour les sites indiqués : `about:config` › `network.automatic-ntlm-auth.trusted-uris`, ou pour un parc la stratégie Firefox *Authentication*. Une page d'accueil le lui explique avant toute demande de mot de passe.
- **NTLM ou Kerberos** : NTLM fonctionne quel que soit le compte du serveur. Kerberos (`Negotiate`) exige que ce compte porte le SPN `HTTP/<serveur>` (service sous le compte machine, ou `setspn -S HTTP/vm-ged CLINIQUES\svc-ged` par un admin du domaine), sinon l'identification échoue.

**Ce que chacun voit.** Un document est visible si la personne peut **lire** le fichier, droits évalués comme Windows le fait (la première entrée qui concerne l'un de ses groupes décide : un refus explicite l'emporte sur une autorisation, une autorisation explicite sur un refus hérité). Un document hors de ses droits répond « Document inconnu », comme s'il n'existait pas ; un document dont les droits n'ont pas pu être lus est masqué. Si rien n'est visible, l'accueil dit pourquoi (aucun droit, droits pas encore enregistrés, index vide). Les administrateurs de la GED voient tout, et sont seuls à voir **État de l'index**.

**Droits des partages.** Windows applique aussi ceux du partage (le plus restrictif l'emporte). Ils sont relus à chaque indexation s'ils sont lisibles, ce qui demande d'être administrateur du serveur de fichiers ; sinon seuls les droits NTFS s'appliquent (cas habituel : partage ouvert à tous, sécurité portée par NTFS).

**Groupes.** La page « Mes groupes » lit la hiérarchie dans l'Active Directory (`memberOf`, lisible par tout compte du domaine, gardé une heure en mémoire). Hors domaine, les groupes sont seulement classés (locaux, identités système).

**Limites.** Les droits sont ceux de la dernière indexation ; les entrées conditionnelles (contrôle d'accès dynamique) sont ignorées ; la correction orthographique s'appuie sur le vocabulaire de tous les documents mais ne propose que des mots présents dans des documents visibles.

> **Après la mise à jour d'une installation existante**, lancer `ged indexer` : les fichiers inchangés ne sont pas relus, seuls leurs droits sont enregistrés. D'ici là, les documents restent masqués, sauf aux administrateurs de la GED.

## Utilisation

```powershell
uv run ged preparer            # choisir les dossiers à exclure (page sur http://127.0.0.1:8001)
uv run ged verifier            # vérifier l'installation avant d'indexer
uv run ged indexer             # nouveautés seulement (barre de progression, temps restant)
uv run ged indexer --forcer    # relit tout (le texte OCR des fichiers inchangés est conservé)
uv run ged ocr [--limite 50]   # scans en attente d'OCR, éventuellement par lots
uv run ged recalculer          # réapplique tags et codes sans relire les fichiers
uv run ged analyser            # rapport analyse-taxonomie.md pour construire les tags
uv run ged serveur             # interface sur http://localhost:8000
uv run ged reinitialiser       # vide l'index (confirmation demandée)
```

Dans l'interface : taper quelques mots (`imprimante bloq`, `"message d'erreur"`, `identitovigilance -formation`), affiner avec les filtres, cliquer sur un dossier, un tag ou un nom pour filtrer dessus. **Ouvrir** affiche ou télécharge le fichier, **Copier le chemin** donne le chemin réseau à coller dans l'explorateur.

## Outil d'inventaire

[`outils/inventaire/Inventaire-GED.ps1`](outils/inventaire/Inventaire-GED.ps1) dresse l'état des lieux d'une arborescence avant la mise en place de la GED (PowerShell 5.1+, sans droits admin, lecture seule) : volumes par type, effort d'OCR, ancienneté, doublons, familles de versions, homogénéité des `.docx`. Résultat : un `rapport.txt` et des CSV.

```powershell
powershell -ExecutionPolicy Bypass -File .\outils\inventaire\Inventaire-GED.ps1 `
    -Chemin '\\nas\metier\ged','\\nas\infra\ged' -Approfondi -Sortie C:\Temp\inventaire
```

Pour ne pas tout retaper : copier [`Inventaire-GED.exemple.psd1`](outils/inventaire/Inventaire-GED.exemple.psd1) en `Inventaire-GED.config.psd1` et lancer le script sans paramètre. Aide complète : `Get-Help .\outils\inventaire\Inventaire-GED.ps1 -Full`.

## Développement

```powershell
uv sync                 # dépendances, y compris de développement
uv run pytest           # tests : documents générés à la volée, pas de PostgreSQL nécessaire
```

Les tests SQL du filtrage des droits tournent en plus sur une base de test jetable si `GED_TEST_BASE` est défini (`postgresql://...`), dans une transaction annulée. Aucun document réel n'est nécessaire, ni ne doit être ajouté au dépôt. Captures du README : [`outils/demo`](outils/demo).

```
ged/
├── __main__.py      # ligne de commande
├── config.py        # lecture de config.toml
├── schema.sql       # schéma PostgreSQL (idempotent)
├── extraction.py    # texte et métadonnées par format ; ocr.py : OCR
├── indexeur.py      # parcours des sources, indexation incrémentale
├── taxonomie.py     # tags automatiques ; texte.py : normalisation, code documentaire
├── recherche.py     # plein texte, filtres, correction, suggestions ; extraits.py, arborescence.py
├── droits.py        # droits NTFS et des partages (pywin32)
├── acces.py         # qui peut voir quoi : filtre SQL selon les droits de la personne
├── annuaire.py      # hiérarchie des groupes (Active Directory)
└── web/             # FastAPI, identification Windows (authentification.py), gabarits
outils/              # inventaire, installation sans droits admin, démo et captures
```

## Feuille de route

- [x] **Phase 0** — Inventaire des partages (script PowerShell)
- [x] **Phase 1** — Indexation en lecture seule, OCR, recherche web avec filtres et tags automatiques
- [ ] **Phase 2** — Dépôt de nouveaux documents : dossier « À classer », OCR, proposition de code et de tags, validation humaine, copie dans l'arborescence GED
- [ ] **Phase 3** — Conformité à la procédure des procédures : modèle `.dotx` à champs, génération des nouvelles notes, contrôle de structure, dates de révision
- [ ] **Phase 4** — ~~Identification Windows et filtrage selon les droits NTFS~~ (fait), tags manuels dans l'interface, liens avec la base de connaissances EasyVista, détection des versions, IA (selon la politique du groupe)
