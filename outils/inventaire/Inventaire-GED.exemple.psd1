# Configuration de l'inventaire : copier ce fichier en Inventaire-GED.config.psd1 (même dossier),
# l'adapter, puis lancer simplement :
#   powershell -ExecutionPolicy Bypass -File .\outils\inventaire\Inventaire-GED.ps1
# Un paramètre tapé en console (-Chemin, -Exclure...) l'emporte sur ce fichier.
# Chemins entre apostrophes simples ; un réglage inutile peut être supprimé ou commenté (#).
@{
    # Dossiers à inventorier
    Chemin = @(
        '\nas\metier\ged'
        '\nas\infra\ged'
    )

    # Dossier des résultats (hors des dossiers inventoriés). Sans ce réglage :
    # .\inventaire_AAAAMMJJ_HHMMSS dans le dossier courant
    Sortie = 'C:\Temp\inventaire'

    # Dossiers à ne pas parcourir du tout (format -like, majuscules indifférentes)
    Exclure = @(
        # '\nas\metier\ged\Applis\LogicielX'   # un dossier précis
        # '*\Archives'                              # tous les dossiers « Archives »
    )

    # $true : ouvre les fichiers en lecture seule (doublons exacts, PDF scannés, structure des .docx)
    Approfondi = $false

    # En mode approfondi, PDF plus gros que cette taille non analysés
    TaillePdfMaxMo = 50
}
