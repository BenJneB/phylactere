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

# 2. Python 3.12 et dépendances du serveur (sans les outils de dev)
Write-Step 'Installation de Python 3.12 et des dépendances du serveur'
& uv --directory $ServerDir sync --no-dev --extra cpu
if ($LASTEXITCODE -ne 0) { throw "uv sync a échoué (code $LASTEXITCODE)." }

# 3. Carte graphique NVIDIA (le moteur OCR sera installé au jalon 2)
Write-Step 'Recherche d''une carte NVIDIA'
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    $gpus = & nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
    if ($LASTEXITCODE -eq 0 -and $gpus) {
        foreach ($gpu in $gpus) { Write-Host "    Carte détectée : $gpu" }
    }
    else {
        Write-Host '    nvidia-smi est présent mais ne répond pas : vérifie le pilote NVIDIA.' -ForegroundColor Yellow
    }
}
else {
    Write-Host '    Aucune carte NVIDIA détectée : l''OCR tournera sur le processeur.' -ForegroundColor Yellow
}
Write-Host '    (Le moteur OCR PaddlePaddle n''est pas encore installé par ce script.)'

Write-Host ''
Write-Host 'Installation terminée. Lance le serveur avec :' -ForegroundColor Green
Write-Host '    powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1'
