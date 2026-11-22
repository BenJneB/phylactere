#Requires -Version 5.1
<#
.SYNOPSIS
    Lance le serveur Phylactère (http://127.0.0.1:8765 par défaut) et affiche le jeton.

.DESCRIPTION
    Les arguments sont transmis au serveur, par exemple : .\scripts\start.ps1 --port 8800
    Exécution : powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1
    Arrêt : Ctrl+C.
#>
$ErrorActionPreference = 'Stop'

$ServerDir = Join-Path (Split-Path -Parent $PSScriptRoot) 'server'
$UvBinDir = Join-Path $env:USERPROFILE '.local\bin'

if (-not (Get-Command uv -ErrorAction SilentlyContinue) -and (Test-Path $UvBinDir)) {
    $env:Path = "$UvBinDir;$env:Path"
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'uv est introuvable : lance d''abord .\scripts\install.ps1'
}

# --no-sync : utilise l'environnement préparé par install.ps1 sans le modifier.
& uv --directory $ServerDir run --no-sync python -m phylactere @args
exit $LASTEXITCODE
