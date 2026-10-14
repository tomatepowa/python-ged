# Démonstration et captures d'écran

Jeu de documents **fictifs** (notes techniques, procédures, noms et logiciels métier inventés) pour montrer la GED sans exposer de vrais documents, et refaire les captures du README.

1. Générer les documents :
   ```powershell
   uv run python outils/demo/generer_demo.py C:\GED-demo\fichiers
   ```
2. Créer une base dédiée (par exemple `ged_demo`) et un fichier de configuration à part, avec deux sources :
   ```toml
   [base]
   url = "postgresql://ged:mot-de-passe@localhost:5432/ged_demo"

   [authentification]
   active = false    # captures : Chrome piloté ne s'identifie pas avec la session Windows

   [[sources]]
   nom = "GED Infra"
   chemin = 'C:\GED-demo\fichiers\GED Infra'
   pole = "Infra"

   [[sources]]
   nom = "GED Métier"
   chemin = 'C:\GED-demo\fichiers\GED Métier'
   pole = "Métier"
   ```
3. Indexer puis lancer le serveur :
   ```powershell
   uv run ged -c config.demo.toml init-db
   uv run ged -c config.demo.toml indexer
   uv run ged -c config.demo.toml serveur
   ```
4. Prendre les captures (pilote le Chrome installé) :
   ```powershell
   uv run --with playwright python outils/demo/captures.py http://127.0.0.1:8000 docs\captures
   ```

Les fiches affichent le chemin réel des fichiers, le nom du poste et ses comptes locaux. Pour les captures publiées, ils ont été remplacés dans la base de démonstration par un faux NAS (`\\nas-dsi\ged\...`) et un faux domaine (`CLINIQUES\...`).
