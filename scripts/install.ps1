#Requires -Version 5.1
<#
.SYNOPSIS
    Installe Phylactère sous Windows : uv, Python 3.12 et les dépendances du serveur.

.DESCRIPTION
    À relancer après chaque « git pull » pour mettre les dépendances à jour.
    Exécution : powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
#>
$ErrorActionPreference = 'Stop'

$RootDir = Split-Path -Parent $PSScriptRoot
$ServerDir = Join-Path $RootDir 'server'
$UvBinDir = Join-Path $env:USERPROFILE '.local\bin'

function Write-Step([string]$Message) {
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Test-Uv {
    return [bool](Get-Command uv -ErrorAction SilentlyContinue)
}

# 1. uv (gestionnaire de Python et de dépendances)
if (-not (Test-Uv) -and (Test-Path $UvBinDir)) {
    $env:Path = "$UvBinDir;$env:Path"
}
if (Test-Uv) {
    Write-Step "uv déjà installé : $(uv --version)"
}
else {
    Write-Step 'Installation de uv (script officiel astral.sh)'
    # Windows PowerShell 5.1 n'active pas TLS 1.2 par défaut.
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    # Processus séparé : le script d'astral ne peut pas interrompre celui-ci.
    & powershell -NoProfile -ExecutionPolicy Bypass -Command 'irm https://astral.sh/uv/install.ps1 | iex'
    if ($LASTEXITCODE -ne 0) { throw "L'installation de uv a échoué (code $LASTEXITCODE)." }
    $env:Path = "$UvBinDir;$env:Path"
    if (-not (Test-Uv)) { throw "uv est installé mais introuvable dans $UvBinDir. Ouvre un nouveau terminal et relance ce script." }
    Write-Step "uv installé : $(uv --version)"
}

# 2. Carte NVIDIA : PaddlePaddle GPU si le pilote supporte CUDA 12.6, sinon CPU
$MinCuda = [version]'12.6'
$Extra = 'cpu'
Write-Step 'Recherche d''une carte NVIDIA'
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    $smi = & nvidia-smi | Out-String
    $gpuName = (& nvidia-smi --query-gpu=name --format=csv,noheader | Select-Object -First 1)
    if ($LASTEXITCODE -eq 0 -and $smi -match 'CUDA Version:\s*(\d+)\.(\d+)') {
        $driverCuda = [version]"$($Matches[1]).$($Matches[2])"
        Write-Host "    Carte détectée : $gpuName (le pilote supporte CUDA $driverCuda)"
        if ($driverCuda -ge $MinCuda) {
            $Extra = 'gpu'
        }
        else {
            Write-Host "    Pilote trop ancien (CUDA $MinCuda minimum) : l'OCR tournera sur le processeur." -ForegroundColor Yellow
            Write-Host '    Mets à jour le pilote NVIDIA puis relance ce script pour utiliser la carte.' -ForegroundColor Yellow
        }
    }
    else {
        Write-Host '    nvidia-smi ne répond pas : vérifie le pilote NVIDIA. L''OCR tournera sur le processeur.' -ForegroundColor Yellow
    }
}
else {
    Write-Host '    Aucune carte NVIDIA détectée : l''OCR tournera sur le processeur.' -ForegroundColor Yellow
}

# 3. Python 3.12 et dépendances du serveur (sans les outils de dev)
if ($Extra -eq 'gpu') {
    Write-Step 'Installation de Python 3.12 et des dépendances, avec PaddlePaddle GPU (plusieurs Go au premier lancement)'
}
else {
    Write-Step 'Installation de Python 3.12 et des dépendances, avec PaddlePaddle CPU'
}
& uv --directory $ServerDir sync --no-dev --extra $Extra
if ($LASTEXITCODE -ne 0) { throw "uv sync a échoué (code $LASTEXITCODE)." }

# 4. Vérification : chargement de PaddleOCR (télécharge les modèles) et lecture d'une image de test
Write-Step 'Vérification de l''OCR'
& uv --directory $ServerDir run --no-sync python -m phylactere --check-ocr
if ($LASTEXITCODE -ne 0) { throw "La vérification de l'OCR a échoué (voir les messages ci-dessus)." }

Write-Host ''
Write-Host 'Installation terminée. Lance le serveur avec :' -ForegroundColor Green
Write-Host '    powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1'
