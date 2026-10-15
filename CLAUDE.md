# GED DSI : consignes pour Claude

GED interne de la DSI d'un groupe de cliniques : indexation en lecture seule de partages réseau
(Python + PostgreSQL), recherche web. Voir README.md pour l'architecture et les commandes.

## Confidentialité : à respecter dans tous les cas

Le groupe n'a pas encore de politique sur l'IA. Les documents indexés sont internes.

- **Ne jamais ouvrir les fichiers des partages indexés** (sources de config.toml), ni les
  documents servis par l'interface web.
- **Ne jamais interroger la base** (psql, scripts Python, `ged` hors des commandes listées
  ci-dessous) : elle contient le texte des documents.
- **Ne pas lire config.toml** : il contient le mot de passe de la base.
- **Ne pas lire exclusions.toml** (écrit par `ged preparer`) : chemins internes des partages.
  Ne pas lancer `ged preparer` ni `ged verifier` sur la vraie configuration : c'est à l'utilisateur.
- Ne travailler que sur le code, `taxonomie.toml` (export de la taxonomie) et le rapport
  `analyse-taxonomie.md`. Ne pas lancer `ged exporter-taxonomie` ni `ged importer-taxonomie` :
  ils lisent et écrivent la base, c'est à l'utilisateur.

## Construire ou ajuster la taxonomie

Demande type : « fais la taxonomie » ou « améliore la taxonomie ».

La taxonomie vit en base et se modifie dans la page Tags (aperçu, recalcul en arrière-plan).
`taxonomie.toml` en est l'export : on travaille dessus, puis l'utilisateur le réimporte.

0. Demander à l'utilisateur d'exporter la taxonomie actuelle, pour partir de la base et non
   d'un fichier ancien : `.\outils\installation\GED-Poste.ps1 exporter-taxonomie` (ou bouton
   « Exporter taxonomie.toml » de la page Tags, fichier à placer à la racine du projet).
1. Si `analyse-taxonomie.md` est absent ou ancien, demander à l'utilisateur de le produire et
   de le relire (il peut contenir des noms de personnes) :
   `.\outils\installation\GED-Poste.ps1 analyser` (ou `uv run ged analyser`).
   Ne pas le lancer soi-même sans accord.
2. Lire `analyse-taxonomie.md` et `taxonomie.toml` (son en-tête décrit le format ; un tag
   `= []` existe sans mot-clé : posé seulement à la main ou par dossier).
3. Proposer les changements **avant** de modifier le fichier, sous forme de liste
   « ajout / modification / retrait », avec le nombre de documents concernés d'après le rapport.
   Attendre la validation de l'utilisateur.
4. Règles pour les tags :
   - catégories existantes en priorité : `[logiciel]` (applications, outils), `[secteur]`
     (domaine métier : identité, soins, facturation…), `[type]` (procédure, note technique,
     debug…) ; en créer une autre seulement si le rapport le justifie clairement ;
   - libellés courts, en français, avec majuscule ; un tag doit concerner au moins quelques
     documents (sinon il n'aide pas à filtrer) ;
   - mots-clés en minuscules, sans souci d'accents (ils sont ignorés), cherchés en mot entier ;
   - préfixe `titre:` pour un mot courant dans un texte mais significatif dans un nom de fichier
     ou de dossier (« procédure », « serveur », un sigle court) ;
   - pas de mot-clé trop court ou ambigu sans `titre:` (« ad », « rh », « ins ») ;
   - ignorer les noms de personnes, les identifiants et les mots vides du rapport ;
   - les noms de dossiers récurrents et les dossiers peu couverts sont les meilleures pistes.
5. Après validation : modifier `taxonomie.toml` (ne pas retirer un tag du fichier sans l'avoir
   proposé : il perdrait ses mots-clés à l'import), puis demander à l'utilisateur de lancer
   `.\outils\installation\GED-Poste.ps1 importer-taxonomie` (affiche les différences, demande
   confirmation, puis recalcule les tags), puis `analyser` à nouveau pour vérifier la couverture.
   Pour un ajustement ponctuel, l'utilisateur peut aussi passer par la page Tags et son aperçu.

## Développement

- Code, commentaires et messages en français, comme le code existant.
- Tests : `uv run pytest`. Linter : `uvx ruff check` (règles dans `pyproject.toml`).
- Le SQL reste dans les modules métier (`recherche.py`, `documents.py`, `acces.py`…), pas dans
  `ged/web/app.py` qui ne fait qu'enchaîner requêtes et gabarits.
- Les fichiers des partages ne sont jamais modifiés : l'indexation est en lecture seule.
