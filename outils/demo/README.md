# Démonstration et captures d'écran

Jeu de documents **fictifs** (notes techniques, procédures, noms et logiciels métier inventés) pour montrer la GED sans exposer de vrais documents, et refaire les captures du README.

1. Générer les documents :
   ```powershell
   uv run python outils/demo/generer_demo.py C:\GED-demo\fichiers
   ```
2. Créer une base dédiée (par exemple `ged_demo`), puis lancer le serveur avec un fichier de connexion à part :
   ```powershell
   $env:GED_CONNEXION = 'connexion.demo.toml'
   uv run ged serveur
   ```
   L'assistant d'installation s'ouvre (code affiché dans la console) : base `ged_demo`, puis deux sources,
   `GED Infra` (`C:\GED-demo\fichiers\GED Infra`, pôle Infra) et `GED Métier` (`C:\GED-demo\fichiers\GED Métier`,
   pôle Métier), et l'identification décochée (captures : Chrome piloté ne s'identifie pas avec la session Windows).
3. Indexer (dans une autre console, avec la même variable `GED_CONNEXION`) :
   ```powershell
   uv run ged indexer
   ```
4. Prendre les captures (pilote le Chrome installé) :
   ```powershell
   uv run --with playwright python outils/demo/captures.py http://127.0.0.1:8000 docs\captures
   ```

Les fiches affichent le chemin réel des fichiers, le nom du poste et ses comptes locaux. Pour les captures publiées, ils ont été remplacés dans la base de démonstration par un faux NAS (`\\nas-dsi\ged\...`) et un faux domaine (`CLINIQUES\...`).
