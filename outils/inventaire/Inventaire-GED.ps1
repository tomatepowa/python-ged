#Requires -Version 5.1
<#
.SYNOPSIS
    Inventaire en LECTURE SEULE d'une arborescence documentaire (préparation du projet GED).

.DESCRIPTION
    Parcourt un ou plusieurs dossiers (locaux ou partages réseau \\nas\...) et produit :
      - rapport.txt              : synthèse chiffrée (à montrer tel quel)
      - inventaire_fichiers.csv  : une ligne par fichier (ouvrable dans Excel)
      - par_dossier.csv          : volumétrie par dossier (2 premiers niveaux)
      - doublons.csv             : groupes de fichiers en double
      - familles_versions.csv    : fichiers qui semblent être des versions d'un même document
      - squelettes_docx.csv      : (-Approfondi) structures de .docx regroupées, de la plus fréquente à la plus rare
      - erreurs_acces.csv        : dossiers/fichiers illisibles (droits, chemins trop longs...)

    Garanties :
      - Aucun fichier source n'est créé, modifié, déplacé ou supprimé.
      - Les résultats sont écrits UNIQUEMENT dans le dossier -Sortie, qui ne peut pas
        se trouver à l'intérieur d'un dossier inventorié.
      - Mode par défaut : seules les métadonnées du système de fichiers sont lues
        (nom, taille, dates), les fichiers ne sont pas ouverts.
      - Mode -Approfondi : les fichiers sont ouverts en lecture seule. La date de
        modification n'est jamais touchée ; la "date de dernier accès" peut l'être
        selon la configuration du serveur.

    Ne nécessite pas de droits administrateur.

.PARAMETER Chemin
    Un ou plusieurs dossiers racine à inventorier (ou Chemin dans le fichier de configuration).

.PARAMETER Sortie
    Dossier de résultats. Par défaut : .\inventaire_AAAAMMJJ_HHMMSS dans le dossier courant.

.PARAMETER Approfondi
    Ouvre les fichiers (lecture seule) pour :
      - confirmer les doublons par empreinte SHA-256 (sinon : même nom + même taille) ;
      - repérer les PDF probablement scannés (donc à passer en OCR) ;
      - lire auteur / titre / nombre de pages des fichiers Office (.docx, .xlsx, .pptx) ;
      - relever la structure des .docx (titres, en-tête, code documentaire) pour mesurer
        l'homogénéité des notes techniques.
    Nettement plus lent sur un partage réseau.

.PARAMETER Exclure
    Motifs de chemins à ignorer, au format -like. Ex : '*\Archives\*', '\\nas\metier\Applis\LogicielX'
    Un dossier visé n'est pas parcouru du tout (utile pour les dossiers d'applications).

.PARAMETER Config
    Fichier de configuration (.psd1) qui évite de tout taper en console : Chemin, Sortie,
    Exclure, Approfondi, TaillePdfMaxMo. Par défaut : Inventaire-GED.config.psd1 à côté du
    script, s'il existe (modèle : Inventaire-GED.exemple.psd1). Un paramètre tapé en console
    l'emporte sur le fichier.

.PARAMETER TaillePdfMaxMo
    En mode approfondi, les PDF plus gros que cette taille ne sont pas analysés. Défaut : 50.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\Inventaire-GED.ps1
    (tout est lu dans Inventaire-GED.config.psd1)

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\Inventaire-GED.ps1 -Chemin "$env:USERPROFILE\Documents"

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\Inventaire-GED.ps1 -Chemin '\\nas\metier\ged','\\nas\infra\ged' -Approfondi -Sortie C:\Temp\inventaire
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string[]]$Chemin = @(),

    [string]$Sortie,

    [switch]$Approfondi,

    [string[]]$Exclure = @(),

    [int]$TaillePdfMaxMo = 50,

    [string]$Config
)

# Fichier de configuration : valeurs par défaut, un paramètre tapé en console l'emporte
if (-not $Config) {
    $parDefaut = Join-Path $PSScriptRoot 'Inventaire-GED.config.psd1'
    if (Test-Path -LiteralPath $parDefaut) { $Config = $parDefaut }
}
if ($Config) {
    if (-not (Test-Path -LiteralPath $Config)) { throw "Fichier de configuration introuvable : $Config" }
    $reglages = Import-PowerShellDataFile -LiteralPath $Config
    $autorises = 'Chemin', 'Sortie', 'Approfondi', 'Exclure', 'TaillePdfMaxMo'
    foreach ($cle in $reglages.Keys) {
        if ($autorises -notcontains $cle) { throw "Réglage inconnu dans $Config : $cle (attendus : $($autorises -join ', '))" }
        if ($PSBoundParameters.ContainsKey($cle)) { continue }
        switch ($cle) {
            'Chemin'         { $Chemin = @($reglages.Chemin) }
            'Sortie'         { $Sortie = [string]$reglages.Sortie }
            'Approfondi'     { $Approfondi = [bool]$reglages.Approfondi }
            'Exclure'        { $Exclure = @($reglages.Exclure) }
            'TaillePdfMaxMo' { $TaillePdfMaxMo = [int]$reglages.TaillePdfMaxMo }
        }
    }
    Write-Host "Configuration : $Config"
}
if (-not $Chemin.Count) {
    throw "Aucun dossier à inventorier : passez -Chemin, ou renseignez Chemin dans Inventaire-GED.config.psd1 (modèle : Inventaire-GED.exemple.psd1)."
}

$debut = Get-Date
$encodage = if ($PSVersionTable.PSVersion.Major -ge 6) { 'utf8BOM' } else { 'UTF8' }   # BOM pour qu'Excel lise les accents
Add-Type -AssemblyName System.IO.Compression.FileSystem

# ============================================================================
# Référentiels
# ============================================================================

$famillesExtensions = [ordered]@{
    'Word'       = '.docx', '.doc', '.docm', '.dotx', '.dot', '.odt', '.rtf'
    'PDF'        = '.pdf'
    'Image'      = '.jpg', '.jpeg', '.png', '.tif', '.tiff', '.bmp', '.gif', '.heic', '.webp'
    'Excel'      = '.xlsx', '.xls', '.xlsm', '.ods', '.csv'
    'PowerPoint' = '.pptx', '.ppt', '.odp'
    'Visio'      = '.vsdx', '.vsd'
    'Mail'       = '.msg', '.eml'
    'Texte'      = '.txt', '.md', '.log', '.xml', '.json', '.ini', '.html', '.htm'
    'Script'     = '.ps1', '.psm1', '.bat', '.cmd', '.vbs', '.sql', '.py', '.sh', '.reg'
    'Archive'    = '.zip', '.7z', '.rar', '.gz', '.tar'
    'Video'      = '.mp4', '.mov', '.avi', '.mkv', '.wmv'
    'Sensible'   = '.kdbx', '.kdb', '.pfx', '.p12', '.key', '.pem', '.ppk'
}
$familleParExtension = @{}
foreach ($famille in $famillesExtensions.Keys) {
    foreach ($ext in $famillesExtensions[$famille]) { $familleParExtension[$ext] = $famille }
}

# Ce qu'il faudra faire de chaque famille pour l'indexer en GED
$indexationParFamille = @{
    'Word'       = 'Texte extractible'
    'Excel'      = 'Texte extractible'
    'PowerPoint' = 'Texte extractible'
    'Visio'      = 'Texte extractible'
    'Mail'       = 'Texte extractible'
    'Texte'      = 'Texte extractible'
    'Script'     = 'Texte extractible'
    'PDF'        = 'A verifier (texte ou scan)'
    'Image'      = 'OCR necessaire'
    'Archive'    = 'Non indexe (archive)'
    'Video'      = 'Non indexe (video)'
    'Sensible'   = 'EXCLU (coffre / cle / certificat)'
    'Autre'      = 'Non indexe'
}

$extensionsOffice = '.docx', '.docm', '.xlsx', '.xlsm', '.pptx'
$extensionsWordXml = '.docx', '.docm'
$nsWord = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'

# Code documentaire du type DSI-INFRA-PRO-012-v03 (à ajuster quand la règle de la procédure des procédures sera formalisée)
$motifCode = '\b[A-Z]{2,6}(?:-[A-Z0-9]{2,8}){1,3}-\d{2,4}(?:-[vV]?\d{1,3})?\b'
$nomsBruit        = 'thumbs.db', 'desktop.ini', '.ds_store'

# Dossiers techniques ignorés d'office : corbeilles et vignettes de NAS (Synology, QNAP), instantanés,
# historiques de synchronisation. Ce ne sont pas des documents.
$nomsDossiersSysteme = '#recycle', '@eadir', '@recycle', '.@__thumb', '#snapshot', '~snapshot', '.snapshot',
                       '.stversions', '$recycle.bin', 'system volume information'
$extensionsBruit  = '.tmp', '.lnk', '.url',
                    # Programmes et bibliothèques (comme l'indexeur de la GED) : aucun intérêt documentaire
                    '.exe', '.dll', '.sys', '.ocx', '.drv', '.cpl', '.msi', '.msp', '.cab', '.jar', '.war',
                    '.class', '.so', '.pyc', '.pyo', '.pdb', '.obj', '.o', '.lib', '.a'

# Marqueurs de version dans un nom de fichier (appliqué sur un nom en minuscules sans accents)
$motifVersion = '(?:^|[\s_\-\.\(])(?:v|ver|version|rev|indice)\s*[\._]?\s*\d+(?:[\._]\d+)*' +
                '|\b(?:final|finale|def|definitif|definitive|old|ancien|ancienne|bak|backup|sauv|copie|copy|brouillon|draft|maj|new|nouveau|nouvelle)\b' +
                '|\(\d+\)' +
                '|\b\d{4}[\-_\.]\d{2}[\-_\.]\d{2}\b|\b\d{2}[\-_\.]\d{2}[\-_\.]\d{4}\b|\b20\d{6}\b'

# ============================================================================
# Fonctions
# ============================================================================

function Remove-Accents([string]$texte) {
    $decompose = $texte.Normalize([Text.NormalizationForm]::FormD)
    $sb = New-Object System.Text.StringBuilder
    foreach ($c in $decompose.ToCharArray()) {
        if ([Globalization.CharUnicodeInfo]::GetUnicodeCategory($c) -ne [Globalization.UnicodeCategory]::NonSpacingMark) {
            [void]$sb.Append($c)
        }
    }
    $sb.ToString().Normalize([Text.NormalizationForm]::FormC)
}

# Nom "de famille" d'un document : sans accents, sans marqueurs de version, sans ponctuation.
# "Procédure_Imprimantes_v3 FINAL" et "procedure imprimantes v2" donnent tous deux "procedure imprimantes".
function Get-NomNormalise([string]$nomSansExtension) {
    $s = Remove-Accents $nomSansExtension.ToLowerInvariant()
    $s = [regex]::Replace($s, $motifVersion, ' ')
    $s = $s -replace '[^a-z0-9]+', ' '
    $s.Trim()
}

function Format-Taille([double]$octets) {
    if ($octets -ge 1GB) { '{0:N2} Go' -f ($octets / 1GB) }
    elseif ($octets -ge 1MB) { '{0:N1} Mo' -f ($octets / 1MB) }
    elseif ($octets -ge 1KB) { '{0:N0} Ko' -f ($octets / 1KB) }
    else { '{0} o' -f $octets }
}

function Get-Anciennete([datetime]$date) {
    $jours = ($debut - $date).TotalDays
    if ($jours -lt 365) { '1. moins de 1 an' }
    elseif ($jours -lt 3 * 365) { '2. de 1 a 3 ans' }
    elseif ($jours -lt 5 * 365) { '3. de 3 a 5 ans' }
    else { '4. plus de 5 ans' }
}

# Regroupement rapide (Group-Object est très lent en PowerShell 5.1 sur de gros volumes)
function Group-ParCle($elements, [scriptblock]$cle) {
    $groupes = New-Object 'System.Collections.Generic.Dictionary[string,System.Collections.Generic.List[object]]'
    foreach ($e in $elements) {
        $k = [string](& $cle $e)
        $liste = $null
        if (-not $groupes.TryGetValue($k, [ref]$liste)) {
            $liste = New-Object System.Collections.Generic.List[object]
            $groupes[$k] = $liste
        }
        $liste.Add($e)
    }
    , $groupes
}

function Measure-Somme($elements, [string]$propriete) {
    $total = 0.0
    foreach ($e in $elements) { $total += $e.$propriete }
    $total
}

# Heuristique : un PDF sans police est très probablement un scan (image seule).
# Les PDF récents peuvent compresser leurs dictionnaires (/ObjStm) : on le signale comme indéterminé.
function Test-PdfScanne([string]$chemin, [long]$taille) {
    if ($taille -gt $TaillePdfMaxMo * 1MB) { return 'Non analyse (trop gros)' }
    try {
        $octets = [System.IO.File]::ReadAllBytes($chemin)
        $contenu = [System.Text.Encoding]::GetEncoding(28591).GetString($octets)
        if ($contenu.Contains('/Font')) { 'Non' }
        elseif ($contenu.Contains('/ObjStm')) { 'Indetermine' }
        elseif ($contenu.Contains('/Image')) { 'Oui' }
        else { 'Indetermine' }
    }
    catch { 'Erreur lecture' }
}

function Get-TexteXml($xml, [string]$xpath, $espaces) {
    $noeud = $xml.SelectSingleNode($xpath, $espaces)
    if ($noeud) { $noeud.InnerText.Trim() } else { '' }
}

function Read-EntreeZip($zip, [string]$nom) {
    $entree = $zip.GetEntry($nom)
    if (-not $entree) { return $null }
    $lecteur = New-Object System.IO.StreamReader($entree.Open())
    try { [xml]$lecteur.ReadToEnd() } finally { $lecteur.Dispose() }
}

function Get-TexteParagraphe($paragraphe, $ns) {
    (@($paragraphe.SelectNodes('.//w:t', $ns)) | ForEach-Object { $_.InnerText }) -join ''
}

# Structure d'un .docx : suite des titres de niveau 1 (le "squelette"), texte de l'en-tête,
# code documentaire éventuel. Sert à mesurer la dérive de structure entre documents copiés les uns des autres.
function Get-StructureDocx($zip) {
    $structure = @{ Squelette = ''; NbTitres = 0; EnTete = ''; CodeDetecte = '' }

    $textesEnTete = foreach ($entree in @($zip.Entries | Where-Object { $_.FullName -like 'word/header*.xml' })) {
        $xml = Read-EntreeZip $zip $entree.FullName
        $ns = New-Object System.Xml.XmlNamespaceManager($xml.NameTable)
        $ns.AddNamespace('w', $nsWord)
        foreach ($p in $xml.SelectNodes('//w:p', $ns)) { Get-TexteParagraphe $p $ns }
    }
    $enTete = ((@($textesEnTete) -join ' ') -replace '\s+', ' ').Trim()
    $structure.EnTete = if ($enTete.Length -gt 150) { $enTete.Substring(0, 150) } else { $enTete }

    $document = Read-EntreeZip $zip 'word/document.xml'
    if (-not $document) { return $structure }
    $ns = New-Object System.Xml.XmlNamespaceManager($document.NameTable)
    $ns.AddNamespace('w', $nsWord)

    $titresNiveau1 = New-Object System.Collections.Generic.List[string]
    $debutTexte = New-Object System.Collections.Generic.List[string]
    foreach ($p in $document.SelectNodes('//w:body//w:p', $ns)) {
        $texte = Get-TexteParagraphe $p $ns
        if ($debutTexte.Count -lt 60 -and $texte) { $debutTexte.Add($texte) }

        $stylePara = $p.SelectSingleNode('w:pPr/w:pStyle', $ns)
        if (-not $stylePara) { continue }
        $style = $stylePara.GetAttribute('val', $nsWord)
        if ($style -notmatch '^(?:Titre|Heading)(\d)$') { continue }
        $structure.NbTitres++
        if ($Matches[1] -ne '1') { continue }

        # "1. Objet du document" -> "objet du document"
        $titre = (Remove-Accents $texte.ToLowerInvariant()) -replace '^\s*(?:\d+(?:\.\d+)*|[ivx]+)\s*[\.\)\-:]?\s+', ''
        $titre = ($titre -replace '\s+', ' ').Trim()
        if ($titre.Length -gt 40) { $titre = $titre.Substring(0, 40) }
        if ($titre) { $titresNiveau1.Add($titre) }
    }

    $structure.Squelette = if ($titresNiveau1.Count) { $titresNiveau1 -join ' | ' } else { '(aucun titre structure)' }
    $code = [regex]::Match($enTete + ' ' + ($debutTexte -join ' '), $motifCode)
    if ($code.Success) { $structure.CodeDetecte = $code.Value }
    $structure
}

# Propriétés d'un fichier Office (docProps/core.xml et app.xml), ouvert en lecture seule
function Get-ProprietesOffice([string]$chemin) {
    $zip = $null
    try {
        $zip = [System.IO.Compression.ZipFile]::OpenRead($chemin)
        $proprietes = @{ Auteur = ''; DernierModificateur = ''; Titre = ''; Pages = ''; Modele = '' }

        $core = Read-EntreeZip $zip 'docProps/core.xml'
        if ($core) {
            $ns = New-Object System.Xml.XmlNamespaceManager($core.NameTable)
            $ns.AddNamespace('dc', 'http://purl.org/dc/elements/1.1/')
            $ns.AddNamespace('cp', 'http://schemas.openxmlformats.org/package/2006/metadata/core-properties')
            $proprietes.Auteur = Get-TexteXml $core '//dc:creator' $ns
            $proprietes.DernierModificateur = Get-TexteXml $core '//cp:lastModifiedBy' $ns
            $proprietes.Titre = Get-TexteXml $core '//dc:title' $ns
        }

        $app = Read-EntreeZip $zip 'docProps/app.xml'
        if ($app) {
            $ns = New-Object System.Xml.XmlNamespaceManager($app.NameTable)
            $ns.AddNamespace('ep', 'http://schemas.openxmlformats.org/officeDocument/2006/extended-properties')
            $proprietes.Pages = Get-TexteXml $app '//ep:Pages' $ns
            $proprietes.Modele = Get-TexteXml $app '//ep:Template' $ns
        }

        if ($extensionsWordXml -contains [IO.Path]::GetExtension($chemin).ToLowerInvariant()) {
            $proprietes += Get-StructureDocx $zip
        }
        $proprietes
    }
    catch { $null }
    finally { if ($zip) { $zip.Dispose() } }
}

# Fichiers sous $racine, sans descendre dans les dossiers système ni dans les dossiers exclus
# (Get-ChildItem -Recurse parcourrait tout avant de filtrer : très lent sur un dossier
# d'application aux milliers de sous-dossiers). Les dossiers illisibles vont dans $erreurs.
function Get-FichiersParcourus([string]$racine) {
    $pile = New-Object System.Collections.Generic.Stack[System.IO.DirectoryInfo]
    $pile.Push((New-Object System.IO.DirectoryInfo $racine))
    $chrono = [System.Diagnostics.Stopwatch]::StartNew()
    while ($pile.Count) {
        $dossier = $pile.Pop()
        # Au plus 4 fois par seconde : Write-Progress est très lent sous PowerShell 5.1
        if ($chrono.ElapsedMilliseconds -ge 250) {
            Write-Progress -Activity "Inventaire de $racine" -Status "$($script:compteur) fichiers parcourus" -CurrentOperation $dossier.FullName
            $chrono.Restart()
        }
        try {
            $contenu = $dossier.GetFiles()
            $sousDossiers = $dossier.GetDirectories()
        }
        catch {
            $exception = if ($_.Exception.InnerException) { $_.Exception.InnerException } else { $_.Exception }
            $script:erreurs.Add([pscustomobject]@{ Chemin = $dossier.FullName; Erreur = $exception.Message })
            continue
        }
        $contenu
        # Ordre alphabétique de parcours : la pile ressort le dernier empilé en premier
        foreach ($sous in @($sousDossiers | Sort-Object Name -Descending)) {
            # Jonctions et liens symboliques : déjà visibles ailleurs, ou boucle possible
            if ($sous.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { continue }
            if ($nomsDossiersSysteme -contains $sous.Name.ToLowerInvariant()) { $script:nbDossiersSysteme++; continue }
            $exclu = $false
            foreach ($motif in $Exclure) {
                if ($sous.FullName -like $motif -or ($sous.FullName + '\') -like $motif) { $exclu = $true; break }
            }
            if ($exclu) { $script:nbDossiersExclus++; continue }
            $pile.Push($sous)
        }
    }
}

function Export-Tableau($donnees, [string]$nomFichier) {
    $chemin = Join-Path $Sortie $nomFichier
    if ($null -eq $donnees -or $donnees.Count -eq 0) {
        Set-Content -LiteralPath $chemin -Value '(aucune ligne)' -Encoding $encodage
        return
    }
    $donnees | Export-Csv -LiteralPath $chemin -NoTypeInformation -Delimiter ';' -Encoding $encodage
}

# ============================================================================
# Contrôles préalables
# ============================================================================

$racines = New-Object System.Collections.Generic.List[string]
foreach ($c in $Chemin) {
    if (-not (Test-Path -LiteralPath $c -PathType Container)) {
        Write-Warning "Dossier introuvable ou inaccessible, ignoré : $c"
        continue
    }
    $racines.Add((Resolve-Path -LiteralPath $c).ProviderPath.TrimEnd('\'))
}
if ($racines.Count -eq 0) { throw 'Aucun dossier valide à inventorier.' }

if (-not $Sortie) { $Sortie = Join-Path (Get-Location).ProviderPath ('inventaire_' + $debut.ToString('yyyyMMdd_HHmmss')) }
$Sortie = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Sortie).TrimEnd('\')

foreach ($racine in $racines) {
    if ($Sortie -eq $racine -or $Sortie.StartsWith($racine + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Le dossier de sortie ($Sortie) est à l'intérieur d'un dossier inventorié ($racine). Choisissez un autre emplacement avec -Sortie : ce script ne doit rien écrire dans les dossiers analysés."
    }
}
New-Item -ItemType Directory -Path $Sortie -Force | Out-Null

# ============================================================================
# 1. Parcours des dossiers (métadonnées uniquement)
# ============================================================================

$fichiers = New-Object System.Collections.Generic.List[object]
$nbDossiersSysteme = 0
$nbDossiersExclus = 0
$erreurs = New-Object System.Collections.Generic.List[object]

foreach ($racine in $racines) {
    $compteur = 0

    Get-FichiersParcourus $racine |
        ForEach-Object {
            $f = $_
            $compteur++

            foreach ($motif in $Exclure) {
                if ($f.FullName -like $motif) { return }
            }

            $extension = $f.Extension.ToLowerInvariant()
            $relatif = $f.FullName.Substring($racine.Length).TrimStart('\')
            $parties = $relatif.Split('\')
            $niveau1 = if ($parties.Count -gt 1) { $parties[0] } else { '(racine)' }
            $niveau2 = if ($parties.Count -gt 2) { $parties[0] + '\' + $parties[1] } else { $niveau1 }

            $famille = $familleParExtension[$extension]
            if (-not $famille) { $famille = 'Autre' }

            $nomMinuscule = $f.Name.ToLowerInvariant()
            $estBruit = $nomMinuscule.StartsWith('~$') -or ($nomsBruit -contains $nomMinuscule) -or ($extensionsBruit -contains $extension)
            $nomSansExtension = [IO.Path]::GetFileNameWithoutExtension($f.Name)
            $indiceVersion = [regex]::IsMatch((Remove-Accents $nomSansExtension.ToLowerInvariant()), $motifVersion)

            $fichiers.Add([pscustomobject]@{
                Racine              = $racine
                Niveau1             = $niveau1
                Niveau2             = $niveau2
                CheminRelatif       = $relatif
                Nom                 = $f.Name
                Extension           = $extension
                Famille             = $famille
                TailleOctets        = $f.Length
                TailleMo            = [math]::Round($f.Length / 1MB, 3)
                Cree                = $f.CreationTime.ToString('yyyy-MM-dd HH:mm')
                Modifie             = $f.LastWriteTime.ToString('yyyy-MM-dd HH:mm')
                Anciennete          = Get-Anciennete $f.LastWriteTime
                Profondeur          = $parties.Count - 1
                CheminLong          = if ($f.FullName.Length -ge 260) { 'Oui' } else { 'Non' }
                Cache               = if ($f.Attributes -band [IO.FileAttributes]::Hidden) { 'Oui' } else { 'Non' }
                Bruit               = if ($estBruit) { 'Oui' } else { 'Non' }
                IndiceVersion       = if ($indiceVersion) { 'Oui' } else { 'Non' }
                NomNormalise        = Get-NomNormalise $nomSansExtension
                Indexation          = $indexationParFamille[$famille]
                PdfScanne           = ''
                Auteur              = ''
                DernierModificateur = ''
                Titre               = ''
                Pages               = ''
                Modele              = ''
                Squelette           = ''
                NbTitres            = ''
                EnTete              = ''
                CodeDetecte         = ''
                Empreinte           = ''
                Dossier             = $f.DirectoryName
                CheminComplet       = $f.FullName
            })
        }

    Write-Progress -Activity "Inventaire de $racine" -Completed
}

$utiles = @($fichiers | Where-Object { $_.Bruit -eq 'Non' })
$lignesSquelettes = $null

# ============================================================================
# 2. Analyse approfondie (ouverture des fichiers en lecture seule)
# ============================================================================

if ($Approfondi) {
    $aAnalyser = @($utiles | Where-Object { $_.Famille -eq 'PDF' -or $extensionsOffice -contains $_.Extension })
    $i = 0
    foreach ($r in $aAnalyser) {
        $i++
        if ($i % 20 -eq 0) {
            Write-Progress -Activity 'Analyse du contenu (PDF, Office)' -Status "$i / $($aAnalyser.Count)" -PercentComplete (100 * $i / $aAnalyser.Count)
        }
        if ($r.Famille -eq 'PDF') {
            $r.PdfScanne = Test-PdfScanne $r.CheminComplet $r.TailleOctets
            if ($r.PdfScanne -eq 'Non') { $r.Indexation = 'Texte extractible' }
            elseif ($r.PdfScanne -eq 'Oui') { $r.Indexation = 'OCR necessaire' }
        }
        else {
            $p = Get-ProprietesOffice $r.CheminComplet
            if ($p) {
                $r.Auteur = $p.Auteur
                $r.DernierModificateur = $p.DernierModificateur
                $r.Titre = $p.Titre
                $r.Pages = $p.Pages
                $r.Modele = $p.Modele
                if ($p.ContainsKey('Squelette')) {
                    $r.Squelette = $p.Squelette
                    $r.NbTitres = $p.NbTitres
                    $r.EnTete = $p.EnTete
                    $r.CodeDetecte = $p.CodeDetecte
                }
            }
        }
    }
    Write-Progress -Activity 'Analyse du contenu (PDF, Office)' -Completed
}

# ============================================================================
# 3. Doublons
# ============================================================================

$candidatsDoublons = @($utiles | Where-Object { $_.TailleOctets -gt 0 })

if ($Approfondi) {
    # On ne calcule l'empreinte que des fichiers qui partagent leur taille avec un autre
    $parTaille = Group-ParCle $candidatsDoublons { param($x) $x.TailleOctets }
    $aHacher = @(foreach ($liste in $parTaille.Values) { if ($liste.Count -gt 1) { $liste } })
    $i = 0
    foreach ($r in $aHacher) {
        $i++
        if ($i % 20 -eq 0) {
            Write-Progress -Activity 'Calcul des empreintes (doublons)' -Status "$i / $($aHacher.Count)" -PercentComplete (100 * $i / $aHacher.Count)
        }
        try { $r.Empreinte = (Get-FileHash -LiteralPath $r.CheminComplet -Algorithm SHA256 -ErrorAction Stop).Hash }
        catch { $erreurs.Add([pscustomobject]@{ Chemin = $r.CheminComplet; Erreur = "Empreinte : $($_.Exception.Message)" }) }
    }
    Write-Progress -Activity 'Calcul des empreintes (doublons)' -Completed
    $groupesDoublons = Group-ParCle @($aHacher | Where-Object { $_.Empreinte }) { param($x) $x.Empreinte }
    $methodeDoublons = 'confirmes par empreinte SHA-256'
}
else {
    $groupesDoublons = Group-ParCle $candidatsDoublons { param($x) $x.Nom.ToLowerInvariant() + '|' + $x.TailleOctets }
    $methodeDoublons = 'probables : meme nom + meme taille (utiliser -Approfondi pour confirmer)'
}

$lignesDoublons = New-Object System.Collections.Generic.List[object]
$nbGroupesDoublons = 0
$nbFichiersRedondants = 0
$espaceRedondant = 0.0
foreach ($liste in $groupesDoublons.Values) {
    if ($liste.Count -lt 2) { continue }
    $nbGroupesDoublons++
    $nbFichiersRedondants += $liste.Count - 1
    $espaceRedondant += $liste[0].TailleOctets * ($liste.Count - 1)
    foreach ($r in $liste) {
        $lignesDoublons.Add([pscustomobject]@{
            Groupe        = $nbGroupesDoublons
            Exemplaires   = $liste.Count
            Nom           = $r.Nom
            TailleOctets  = $r.TailleOctets
            Modifie       = $r.Modifie
            CheminComplet = $r.CheminComplet
        })
    }
}

# ============================================================================
# 4. Familles de versions (même nom normalisé, au moins un marqueur de version)
# ============================================================================

$groupesNoms = Group-ParCle @($utiles | Where-Object { $_.NomNormalise }) { param($x) $x.Famille + '|' + $x.NomNormalise }
$lignesVersions = New-Object System.Collections.Generic.List[object]
$nbFamillesVersions = 0
foreach ($liste in $groupesNoms.Values) {
    if ($liste.Count -lt 2) { continue }
    if (@($liste | Where-Object { $_.IndiceVersion -eq 'Oui' }).Count -eq 0) { continue }
    if (@($liste | ForEach-Object { $_.Nom } | Sort-Object -Unique).Count -lt 2) { continue }
    $nbFamillesVersions++
    $tries = @($liste | Sort-Object Modifie -Descending)
    for ($j = 0; $j -lt $tries.Count; $j++) {
        $lignesVersions.Add([pscustomobject]@{
            FamilleVersions = $nbFamillesVersions
            NomCommun       = $tries[$j].NomNormalise
            NbFichiers      = $tries.Count
            PlusRecent      = if ($j -eq 0) { 'Oui' } else { 'Non' }
            Nom             = $tries[$j].Nom
            Modifie         = $tries[$j].Modifie
            CheminComplet   = $tries[$j].CheminComplet
        })
    }
}

# ============================================================================
# 5. Volumétrie par dossier
# ============================================================================

$parDossier = Group-ParCle $utiles { param($x) $x.Racine + '|' + $x.Niveau2 }
$lignesDossiers = foreach ($liste in $parDossier.Values) {
    $premier = $liste[0]
    [pscustomobject]@{
        Racine           = $premier.Racine
        Dossier          = $premier.Niveau2
        NbFichiers       = $liste.Count
        VolumeMo         = [math]::Round((Measure-Somme $liste 'TailleOctets') / 1MB, 1)
        Word             = @($liste | Where-Object { $_.Famille -eq 'Word' }).Count
        PDF              = @($liste | Where-Object { $_.Famille -eq 'PDF' }).Count
        Image            = @($liste | Where-Object { $_.Famille -eq 'Image' }).Count
        Autres           = @($liste | Where-Object { 'Word', 'PDF', 'Image' -notcontains $_.Famille }).Count
        DerniereModif    = ($liste | ForEach-Object { $_.Modifie } | Sort-Object -Descending | Select-Object -First 1)
    }
}
$lignesDossiers = @($lignesDossiers | Sort-Object Racine, Dossier)

# ============================================================================
# 6. Rapport de synthèse
# ============================================================================

$total = $utiles.Count
$volumeTotal = Measure-Somme $utiles 'TailleOctets'
$L = New-Object System.Collections.Generic.List[string]

function Add-Section([string]$titre) {
    $L.Add('')
    $L.Add($titre)
    $L.Add(('-' * $titre.Length))
}

# Par défaut tri par nombre décroissant ; -ParCle pour un tri alphabétique
function Add-Repartition($groupes, [switch]$ParCle) {
    if ($ParCle) { $cles = @($groupes.Keys | Sort-Object) }
    else { $cles = @($groupes.Keys | Sort-Object { $groupes[$_].Count } -Descending) }
    foreach ($k in $cles) {
        $liste = $groupes[$k]
        $pourcentage = if ($total) { 100 * $liste.Count / $total } else { 0 }
        $L.Add(('  {0,-32} {1,8} fichiers  {2,5:N1} %  {3,12}' -f $k, $liste.Count, $pourcentage, (Format-Taille (Measure-Somme $liste 'TailleOctets'))))
    }
}

$L.Add('INVENTAIRE DOCUMENTAIRE - PREPARATION GED')
$L.Add('=========================================')
$L.Add("Date            : $($debut.ToString('dd/MM/yyyy HH:mm'))")
$L.Add("Mode            : $(if ($Approfondi) { 'approfondi (contenu analyse en lecture seule)' } else { 'rapide (metadonnees uniquement)' })")
foreach ($racine in $racines) { $L.Add("Dossier analyse : $racine") }
if ($Config) { $L.Add("Configuration   : $Config") }
if ($Exclure.Count) { $L.Add("Exclusions      : $($Exclure -join ', ')") }

Add-Section 'VUE D''ENSEMBLE'
$L.Add(('  Fichiers utiles             : {0:N0}' -f $total))
$L.Add(('  Volume total                : {0}' -f (Format-Taille $volumeTotal)))
$L.Add(('  Dossiers contenant fichiers : {0:N0}' -f @($utiles | ForEach-Object { $_.Dossier } | Sort-Object -Unique).Count))
$L.Add(('  Fichiers parasites ignores  : {0:N0}  (~$ temporaires Office, Thumbs.db, raccourcis, programmes .exe/.dll...)' -f ($fichiers.Count - $total)))
$L.Add(('  Dossiers systeme ignores    : {0:N0}  (corbeilles et vignettes NAS, snapshots, .stversions...)' -f $nbDossiersSysteme))
if ($Exclure.Count) { $L.Add(('  Dossiers exclus (non lus)   : {0:N0}' -f $nbDossiersExclus)) }
$L.Add(('  Erreurs d''acces             : {0:N0}  (voir erreurs_acces.csv)' -f $erreurs.Count))

Add-Section 'PAR TYPE DE DOCUMENT'
Add-Repartition (Group-ParCle $utiles { param($x) $x.Famille })

Add-Section 'EFFORT D''INDEXATION'
Add-Repartition (Group-ParCle $utiles { param($x) $x.Indexation })
if (-not $Approfondi) { $L.Add('  (les PDF "a verifier" seront tries texte / scan avec -Approfondi)') }

Add-Section 'ANCIENNETE (date de derniere modification)'
Add-Repartition (Group-ParCle $utiles { param($x) $x.Anciennete }) -ParCle

Add-Section 'PAR DOSSIER DE PREMIER NIVEAU'
Add-Repartition (Group-ParCle $utiles { param($x) $x.Niveau1 })

if ($Approfondi) {
    $docxAnalyses = @($utiles | Where-Object { $_.Squelette })
    if ($docxAnalyses.Count) {
        $n = $docxAnalyses.Count
        $avecTitres = @($docxAnalyses | Where-Object { $_.Squelette -ne '(aucun titre structure)' }).Count
        $avecCode = @($docxAnalyses | Where-Object { $_.CodeDetecte }).Count
        $avecEnTete = @($docxAnalyses | Where-Object { $_.EnTete }).Count
        $squelettes = Group-ParCle $docxAnalyses { param($x) $x.Squelette }

        Add-Section 'STRUCTURE DES DOCUMENTS WORD (.docx)'
        $L.Add(('  .docx analyses                       : {0:N0}' -f $n))
        $L.Add(('  Avec des titres structures (styles)  : {0:N0}  ({1:N0} %)' -f $avecTitres, (100 * $avecTitres / $n)))
        $L.Add(('  Avec un en-tete renseigne            : {0:N0}  ({1:N0} %)' -f $avecEnTete, (100 * $avecEnTete / $n)))
        $L.Add(('  Avec un code documentaire detecte    : {0:N0}  ({1:N0} %)' -f $avecCode, (100 * $avecCode / $n)))
        $L.Add(('  Squelettes differents (titres niv.1) : {0:N0}  (voir squelettes_docx.csv)' -f $squelettes.Count))
        $L.Add('  Squelettes les plus frequents :')
        foreach ($k in @($squelettes.Keys | Sort-Object { $squelettes[$_].Count } -Descending | Select-Object -First 10)) {
            $libelle = if ($k.Length -gt 95) { $k.Substring(0, 92) + '...' } else { $k }
            $L.Add(('    {0,5} x  {1}' -f $squelettes[$k].Count, $libelle))
        }

        $lignesSquelettes = foreach ($k in $squelettes.Keys) {
            $liste = @($squelettes[$k] | Sort-Object Modifie)
            [pscustomobject]@{
                Squelette     = $k
                NbDocuments   = $liste.Count
                PlusAncien    = $liste[0].Modifie
                PlusRecent    = $liste[-1].Modifie
                ExempleRecent = $liste[-1].CheminComplet
            }
        }
        $lignesSquelettes = @($lignesSquelettes | Sort-Object NbDocuments -Descending)
    }
}

Add-Section 'EXTENSIONS LES PLUS FREQUENTES'
$parExtension = Group-ParCle $utiles { param($x) if ($x.Extension) { $x.Extension } else { '(sans extension)' } }
foreach ($k in @($parExtension.Keys | Sort-Object { $parExtension[$_].Count } -Descending | Select-Object -First 15)) {
    $L.Add(('  {0,-12} {1,8}' -f $k, $parExtension[$k].Count))
}

Add-Section 'QUALITE DU STOCKAGE'
$L.Add(('  Doublons ({0})' -f $methodeDoublons))
$L.Add(('    Groupes de doublons       : {0:N0}' -f $nbGroupesDoublons))
$L.Add(('    Copies en trop            : {0:N0}  ({1} recuperables)' -f $nbFichiersRedondants, (Format-Taille $espaceRedondant)))
$L.Add(('  Familles de versions        : {0:N0}  (ex : "proc_v2", "proc_v3_final"... voir familles_versions.csv)' -f $nbFamillesVersions))
$L.Add(('  Fichiers avec marqueur de version dans le nom : {0:N0}' -f @($utiles | Where-Object { $_.IndiceVersion -eq 'Oui' }).Count))
$L.Add(('  Chemins >= 260 caracteres   : {0:N0}  (posent probleme a certains outils Windows)' -f @($utiles | Where-Object { $_.CheminLong -eq 'Oui' }).Count))
$L.Add(('  Profondeur max d''arborescence : {0}' -f (@($utiles | ForEach-Object { $_.Profondeur } | Sort-Object -Descending | Select-Object -First 1) + 0)))

Add-Section 'LES 10 PLUS GROS FICHIERS'
foreach ($r in @($utiles | Sort-Object TailleOctets -Descending | Select-Object -First 10)) {
    $L.Add(('  {0,10}  {1}' -f (Format-Taille $r.TailleOctets), $r.CheminRelatif))
}

$duree = (Get-Date) - $debut
$L.Add('')
$L.Add(('Duree : {0:hh\:mm\:ss} - Aucun fichier source n''a ete modifie.' -f $duree))

# ============================================================================
# 7. Écriture des résultats
# ============================================================================

Set-Content -LiteralPath (Join-Path $Sortie 'rapport.txt') -Value $L -Encoding $encodage
Export-Tableau $fichiers 'inventaire_fichiers.csv'
Export-Tableau $lignesDossiers 'par_dossier.csv'
Export-Tableau $lignesDoublons 'doublons.csv'
Export-Tableau $lignesVersions 'familles_versions.csv'
Export-Tableau $erreurs 'erreurs_acces.csv'
if ($lignesSquelettes) { Export-Tableau $lignesSquelettes 'squelettes_docx.csv' }

$L | ForEach-Object { Write-Host $_ }
Write-Host ''
Write-Host "Resultats ecrits dans : $Sortie" -ForegroundColor Green
