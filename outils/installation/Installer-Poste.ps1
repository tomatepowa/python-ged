#Requires -Version 5.1
<#
.SYNOPSIS
    Installe tout le nécessaire pour faire tourner la GED sur un poste, SANS droits administrateur.

.DESCRIPTION
    Tout est installé dans un seul dossier (par défaut %LOCALAPPDATA%\ged), sans rien écrire
    dans le système ni dans la variable PATH :

      1. uv (gestionnaire Python) .......... <Dossier>\outils\uv\uv.exe
      2. Python + dépendances du projet .... téléchargés par uv (dossier .venv du projet)
      3. PostgreSQL (version zip) .......... <Dossier>\postgresql\pgsql, données dans <Dossier>\postgresql\donnees
      4. Fichier de langue OCR français .... <Dossier>\tessdata\fra.traineddata
      5. Base "ged" créée, config.toml du projet généré (mot de passe aléatoire), schéma initialisé

    Le script peut être relancé sans risque : chaque étape déjà faite est sautée.
    PostgreSQL n'est pas installé comme service Windows (il faudrait des droits admin) :
    le démarrer avec GED-Poste.ps1 après un redémarrage du PC.

.PARAMETER Source
    Partage(s) à indexer, au format "Nom|Chemin|Pôle". Répétable.
    Ex : -Source 'GED Infra|\\nas\infra\ged|Infra', 'GED Métier|\\nas\metier\ged|Métier'
    Sans ce paramètre, config.toml contient des exemples à compléter.

.PARAMETER Dossier
    Dossier d'installation. Défaut : %LOCALAPPDATA%\ged

.PARAMETER Port
    Port de PostgreSQL (écoute sur localhost uniquement). Défaut : 5432.
    Si un autre PostgreSQL l'utilise déjà, en choisir un autre (ex. 5433).

.PARAMETER VersionPostgres
    Version des binaires EDB à télécharger. Défaut : 18.4-1

.PARAMETER OcrPrecis
    Télécharge le modèle OCR "best" (≈ 15 Mo, plus précis, plus lent) au lieu de "fast" (≈ 1 Mo).

.PARAMETER Proxy
    Proxy HTTP(S) pour les téléchargements, ex. http://proxy.clinique.local:8080

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\outils\installation\Installer-Poste.ps1 -Source 'GED Infra|\\nas\infra\ged|Infra'
#>
[CmdletBinding()]
param(
    [string[]]$Source = @(),
    [string]$Dossier = (Join-Path $env:LOCALAPPDATA 'ged'),
    [int]$Port = 5432,
    [string]$VersionPostgres = '18.4-1',
    [switch]$OcrPrecis,
    [string]$Proxy
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # accélère fortement les téléchargements en PowerShell 5.1

$projet = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$dossierOutils = Join-Path $Dossier 'outils'
$dossierTelechargements = Join-Path $Dossier 'telechargements'
$dossierPostgres = Join-Path $Dossier 'postgresql'
$binPostgres = Join-Path $dossierPostgres 'pgsql\bin'
$donneesPostgres = Join-Path $dossierPostgres 'donnees'
$dossierTessdata = Join-Path $Dossier 'tessdata'
$uv = Join-Path $dossierOutils 'uv\uv.exe'
$configuration = Join-Path $projet 'config.toml'
# Versions fournies avec Windows, appelées par leur chemin complet : un autre tar.exe dans le PATH
# (celui de Git, par exemple) ne comprend pas les chemins C:\ et ferait échouer la décompression
$tar = Join-Path $env:SystemRoot 'System32\tar.exe'
$curl = Join-Path $env:SystemRoot 'System32\curl.exe'

# ============================================================================
# Fonctions utilitaires
# ============================================================================

function Write-Etape([string]$texte) {
    Write-Host ''
    Write-Host "==> $texte" -ForegroundColor Cyan
}

function Write-Info([string]$texte) { Write-Host "    $texte" }

# Lance un exécutable et s'arrête si son code de retour n'est pas 0
# (PowerShell 5.1 transforme en erreur bloquante tout message écrit par un programme sur sa
#  sortie d'erreur quand la sortie est redirigée, même en cas de succès : on ne se fie qu'au code
#  de retour)
function Invoke-Natif {
    param([string]$Programme, [string[]]$Arguments)
    $ErrorActionPreference = 'Continue'
    & $Programme @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Échec ($LASTEXITCODE) : $Programme $($Arguments -join ' ')" }
}

function Test-Postgres {
    $ErrorActionPreference = 'Continue'
    & $pgCtl status -D $donneesPostgres *> $null
    $LASTEXITCODE -eq 0
}

# Téléchargement avec curl.exe (fourni avec Windows 10/11, respecte HTTPS_PROXY)
function Get-Fichier([string]$Url, [string]$Destination) {
    if (Test-Path -LiteralPath $Destination) {
        Write-Info "Déjà téléchargé : $(Split-Path $Destination -Leaf)"
        return
    }
    Write-Info "Téléchargement de $Url"
    $temporaire = "$Destination.partiel"
    Invoke-Natif $curl @('--location', '--fail', '--silent', '--show-error', '--retry', '3', '--output', $temporaire, $Url)
    Move-Item -LiteralPath $temporaire -Destination $Destination -Force
}

function New-MotDePasse {
    $caracteres = [char[]]'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789'
    $octets = New-Object byte[] 24
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($octets)
    -join ($octets | ForEach-Object { $caracteres[$_ % $caracteres.Length] })
}

function Test-PortLibre([int]$NumeroPort) {
    $ecoute = $null
    try {
        $ecoute = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $NumeroPort)
        $ecoute.Start()
        return $true
    }
    catch { return $false }
    finally { if ($ecoute) { $ecoute.Stop() } }
}

# Chaîne TOML entre apostrophes : aucun échappement (chemins Windows tels quels)
function ConvertTo-TomlLitteral([string]$texte) {
    if ($texte.Contains("'")) { throw "Apostrophe interdite dans : $texte" }
    "'$texte'"
}

# ============================================================================
# Vérifications
# ============================================================================

Write-Etape 'Vérifications'
Write-Info "Projet       : $projet"
Write-Info "Installation : $Dossier"
if (-not (Test-Path (Join-Path $projet 'pyproject.toml'))) {
    throw "pyproject.toml introuvable dans $projet : lancez ce script depuis le dépôt (outils\installation)."
}
foreach ($programme in $curl, $tar) {
    if (-not (Test-Path $programme)) {
        throw "$programme est introuvable (fourni avec Windows 10 1803 et plus récents)."
    }
}
if ($Proxy) {
    $env:HTTPS_PROXY = $Proxy
    $env:HTTP_PROXY = $Proxy
    Write-Info "Proxy : $Proxy"
}
$sourcesAnalysees = foreach ($s in $Source) {
    $parties = $s.Split('|')
    if ($parties.Count -ne 3) { throw "Source mal formée (attendu 'Nom|Chemin|Pôle') : $s" }
    if (-not (Test-Path -LiteralPath $parties[1])) { Write-Warning "Partage inaccessible pour l'instant : $($parties[1])" }
    [pscustomobject]@{ Nom = $parties[0].Trim(); Chemin = $parties[1].Trim(); Pole = $parties[2].Trim() }
}
New-Item -ItemType Directory -Force -Path $dossierOutils, $dossierTelechargements, $dossierPostgres, $dossierTessdata | Out-Null

# ============================================================================
# 1. uv
# ============================================================================

Write-Etape '1/5 uv (gestionnaire Python)'
if (Test-Path $uv) {
    Write-Info "Déjà installé : $(& $uv --version)"
}
else {
    $archive = Join-Path $dossierTelechargements 'uv-x86_64-pc-windows-msvc.zip'
    Get-Fichier 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip' $archive
    $destination = Join-Path $dossierOutils 'uv'
    New-Item -ItemType Directory -Force -Path $destination | Out-Null
    Invoke-Natif $tar @('-xf', $archive, '-C', $destination)
    Write-Info "Installé : $(& $uv --version)"
}

# ============================================================================
# 2. Python et dépendances du projet
# ============================================================================

Write-Etape '2/5 Python et dépendances du projet'
Push-Location $projet
try { Invoke-Natif $uv @('sync', '--no-dev') }
finally { Pop-Location }

# ============================================================================
# 3. PostgreSQL
# ============================================================================

Write-Etape "3/5 PostgreSQL $VersionPostgres (version zip, sans service Windows)"
if (Test-Path (Join-Path $binPostgres 'pg_ctl.exe')) {
    Write-Info 'Binaires déjà présents.'
}
else {
    $archive = Join-Path $dossierTelechargements "postgresql-$VersionPostgres-windows-x64-binaries.zip"
    Get-Fichier "https://get.enterprisedb.com/postgresql/postgresql-$VersionPostgres-windows-x64-binaries.zip" $archive
    Write-Info 'Décompression (pgAdmin, symboles et documentation exclus)...'
    Invoke-Natif $tar @('-xf', $archive, '-C', $dossierPostgres,
        '--exclude', 'pgsql/pgAdmin 4', '--exclude', 'pgsql/symbols', '--exclude', 'pgsql/doc', '--exclude', 'pgsql/StackBuilder')
}

$initialisation = -not (Test-Path (Join-Path $donneesPostgres 'PG_VERSION'))
if ($initialisation) {
    if (Test-Path $configuration) {
        throw "config.toml existe déjà mais la base n'est pas initialisée : renommez config.toml puis relancez."
    }
    if (-not (Test-PortLibre $Port)) {
        throw "Le port $Port est déjà utilisé (autre PostgreSQL ?). Relancez avec -Port 5433 par exemple."
    }
    Write-Info 'Initialisation de la base de données...'
    $motDePasse = New-MotDePasse
    # (New-TemporaryFile n'est pas disponible sur toutes les installations de PowerShell 5.1)
    $fichierMotDePasse = [System.IO.Path]::GetTempFileName()
    try {
        Set-Content -LiteralPath $fichierMotDePasse -Value $motDePasse -NoNewline -Encoding Ascii
        $majeure = [int]($VersionPostgres.Split('.')[0])
        $locale = if ($majeure -ge 17) { @('--locale-provider=builtin', '--builtin-locale=C.UTF-8') } else { @('--no-locale') }
        Invoke-Natif (Join-Path $binPostgres 'initdb.exe') (@('-D', $donneesPostgres, '-U', 'ged', "--pwfile=$fichierMotDePasse",
            '-A', 'scram-sha-256', '-E', 'UTF8') + $locale)
    }
    finally { Remove-Item -LiteralPath $fichierMotDePasse -Force }

    # Écoute locale uniquement, sur le port choisi
    Add-Content -LiteralPath (Join-Path $donneesPostgres 'postgresql.conf') -Encoding Ascii -Value @(
        '', '# Ajouté par Installer-Poste.ps1', "listen_addresses = 'localhost'", "port = $Port"
    )
}
else {
    Write-Info 'Base déjà initialisée.'
    $ligne = Select-String -LiteralPath (Join-Path $donneesPostgres 'postgresql.conf') -Pattern '^port = (\d+)' | Select-Object -Last 1
    if ($ligne) { $Port = [int]$ligne.Matches[0].Groups[1].Value }
}

$pgCtl = Join-Path $binPostgres 'pg_ctl.exe'
if (-not (Test-Postgres)) {
    Write-Info "Démarrage de PostgreSQL (port $Port)..."
    Invoke-Natif $pgCtl @('start', '-D', $donneesPostgres, '-l', (Join-Path $dossierPostgres 'postgresql.log'), '-w', '-t', '60')
}
else {
    Write-Info 'PostgreSQL tourne déjà.'
}

if ($initialisation) {
    $env:PGPASSWORD = $motDePasse
    try {
        Invoke-Natif (Join-Path $binPostgres 'createdb.exe') @('-h', 'localhost', '-p', "$Port", '-U', 'ged', 'ged')
    }
    finally { Remove-Item Env:PGPASSWORD }
    Write-Info 'Base "ged" créée.'
}

# ============================================================================
# 4. Fichier de langue OCR
# ============================================================================

Write-Etape '4/5 Fichier de langue OCR (français)'
$modele = if ($OcrPrecis) { 'tessdata_best' } else { 'tessdata_fast' }
Get-Fichier "https://github.com/tesseract-ocr/$modele/raw/main/fra.traineddata" (Join-Path $dossierTessdata 'fra.traineddata')

# ============================================================================
# 5. Configuration et schéma
# ============================================================================

Write-Etape '5/5 Configuration et schéma de la base'
if ($initialisation) {
    $soffice = @(
        (Join-Path $env:ProgramFiles 'LibreOffice\program\soffice.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\LibreOffice\program\soffice.exe')
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1

    $lignes = @(
        "# Configuration générée par Installer-Poste.ps1 le $(Get-Date -Format 'dd/MM/yyyy HH:mm') - non versionnée",
        '',
        '[base]',
        "url = `"postgresql://ged:$motDePasse@localhost:$Port/ged`"",
        '',
        '[serveur]',
        'hote = "127.0.0.1"',
        'port = 8000',
        '',
        '[indexation]',
        'taille_max_mo = 100',
        'exclure = []',
        '',
        '[ocr]',
        'active = true',
        "tessdata = $(ConvertTo-TomlLitteral $dossierTessdata)",
        'langue = "fra"',
        'dpi = 300',
        '',
        '[interface]',
        '# Exemples affichés dans la barre de recherche vide',
        'exemples_recherche = "imprimante, identitovigilance, \"message d''erreur\""',
        ''
    )
    if ($soffice) {
        $lignes += '[conversion]', "soffice = $(ConvertTo-TomlLitteral $soffice)", ''
    }
    else {
        $lignes += '# LibreOffice introuvable : les anciens .doc / .xls / .ppt ne seront pas lus.', '# [conversion]', "# soffice = 'C:\chemin\vers\soffice.exe'", ''
    }
    if ($sourcesAnalysees) {
        foreach ($s in $sourcesAnalysees) {
            $lignes += '[[sources]]', "nom = `"$($s.Nom)`"", "chemin = $(ConvertTo-TomlLitteral $s.Chemin)", "pole = `"$($s.Pole)`"", ''
        }
    }
    else {
        $lignes += '# Partages à indexer : décommenter et adapter (une section par partage)',
            '# [[sources]]', '# nom = "GED Infra"', "# chemin = '\\nas\infra\ged'", '# pole = "Infra"', ''
    }
    # UTF-8 sans BOM : attendu par le lecteur TOML de Python
    [System.IO.File]::WriteAllLines($configuration, [string[]]$lignes, (New-Object System.Text.UTF8Encoding($false)))
    Write-Info "config.toml généré : $configuration"
}
else {
    Write-Info 'config.toml conservé (base déjà initialisée).'
}

Push-Location $projet
try { Invoke-Natif $uv @('run', 'ged', 'init-db') }
finally { Pop-Location }

# ============================================================================
# Résumé
# ============================================================================

$commandes = Join-Path $PSScriptRoot 'GED-Poste.ps1'
Write-Host ''
Write-Host 'Installation terminée.' -ForegroundColor Green
Write-Host ''
Write-Host '  Ensuite :'
if (-not $sourcesAnalysees) {
    Write-Host "    0. Déclarer les partages à indexer dans $configuration"
}
Write-Host "    1. Indexer      : powershell -ExecutionPolicy Bypass -File `"$commandes`" indexer"
Write-Host "    2. OCR (scans)  : powershell -ExecutionPolicy Bypass -File `"$commandes`" ocr"
Write-Host "    3. Interface    : powershell -ExecutionPolicy Bypass -File `"$commandes`" demarrer   puis http://127.0.0.1:8000"
Write-Host ''
Write-Host "  Après un redémarrage du PC, PostgreSQL ne se relance pas tout seul : `"$commandes`" demarrer s'en charge."
