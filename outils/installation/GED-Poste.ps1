#Requires -Version 5.1
<#
.SYNOPSIS
    Commandes du quotidien pour la GED installée par Installer-Poste.ps1 (sans droits admin).

.DESCRIPTION
    demarrer : démarre PostgreSQL si besoin, puis l'interface web (Ctrl+C pour l'arrêter)
    arreter  : arrête PostgreSQL
    etat     : indique si PostgreSQL tourne
    indexer  : démarre PostgreSQL si besoin, puis indexe les partages de config.toml
    ocr      : démarre PostgreSQL si besoin, puis passe en OCR les scans en attente
    recalculer : réapplique tags et codes (après modification de taxonomie.toml)
    analyser : écrit analyse-taxonomie.md (statistiques pour construire taxonomie.toml)
    reinitialiser : vide l'index pour repartir de zéro (confirmation demandée), puis relancer indexer

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\outils\installation\GED-Poste.ps1 demarrer
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('demarrer', 'arreter', 'etat', 'indexer', 'ocr', 'recalculer', 'reinitialiser', 'analyser')]
    [string]$Action = 'etat',

    [string]$Dossier = (Join-Path $env:LOCALAPPDATA 'ged')
)

$ErrorActionPreference = 'Stop'
$projet = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$pgCtl = Join-Path $Dossier 'postgresql\pgsql\bin\pg_ctl.exe'
$donnees = Join-Path $Dossier 'postgresql\donnees'
$journal = Join-Path $Dossier 'postgresql\postgresql.log'
$uv = Join-Path $Dossier 'outils\uv\uv.exe'

if (-not (Test-Path $pgCtl)) { throw "PostgreSQL introuvable dans $Dossier : lancez d'abord Installer-Poste.ps1." }

# Codes de retour seulement : en PowerShell 5.1, un message sur la sortie d'erreur d'un programme
# devient une erreur bloquante quand la sortie est redirigée
function Test-Postgres {
    $ErrorActionPreference = 'Continue'
    & $pgCtl status -D $donnees *> $null
    $LASTEXITCODE -eq 0
}

function Start-Postgres {
    if (Test-Postgres) { return }
    Write-Host 'Démarrage de PostgreSQL...'
    $ErrorActionPreference = 'Continue'
    & $pgCtl start -D $donnees -l $journal -w -t 60
    if ($LASTEXITCODE -ne 0) { throw "PostgreSQL n'a pas démarré : voir $journal" }
}

function Invoke-Ged([string[]]$Arguments) {
    $ErrorActionPreference = 'Continue'
    Push-Location $projet
    try {
        & $uv run --no-dev ged @Arguments
        if ($LASTEXITCODE -ne 0) { throw "ged $($Arguments -join ' ') a échoué ($LASTEXITCODE)" }
    }
    finally { Pop-Location }
}

switch ($Action) {
    'etat' {
        if (Test-Postgres) { Write-Host 'PostgreSQL : démarré' -ForegroundColor Green }
        else { Write-Host 'PostgreSQL : arrêté' -ForegroundColor Yellow }
    }
    'arreter' {
        if (Test-Postgres) {
            $ErrorActionPreference = 'Continue'
            & $pgCtl stop -D $donnees -m fast -w
            Write-Host 'PostgreSQL arrêté.'
        }
        else { Write-Host 'PostgreSQL était déjà arrêté.' }
    }
    'demarrer' {
        Start-Postgres
        Write-Host 'Interface : http://127.0.0.1:8000  (Ctrl+C pour arrêter)' -ForegroundColor Green
        Invoke-Ged @('serveur')
    }
    default {
        Start-Postgres
        Invoke-Ged @($Action)
    }
}
