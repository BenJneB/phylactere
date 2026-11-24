#Requires -Version 5.1
<#
.SYNOPSIS
    Démarre le serveur Phylactère automatiquement à l'ouverture de session (optionnel).

.DESCRIPTION
    Crée un raccourci dans le dossier Démarrage de l'utilisateur (aucun droit administrateur
    requis) : start.ps1 y est lancé dans une fenêtre cachée. Le serveur est aussi démarré
    tout de suite.
    Journal du serveur : %LOCALAPPDATA%\phylactere\Logs\server.log
    Arrêter le serveur : .\scripts\stop.ps1
    Retirer le démarrage automatique : .\scripts\install-autostart.ps1 -Remove
    Exécution : powershell -ExecutionPolicy Bypass -File .\scripts\install-autostart.ps1
#>
param(
    # Retire le raccourci (le serveur déjà lancé continue de tourner).
    [switch]$Remove
)
$ErrorActionPreference = 'Stop'

$RootDir = Split-Path -Parent $PSScriptRoot
$StartScript = Join-Path $PSScriptRoot 'start.ps1'
$Shortcut = Join-Path ([Environment]::GetFolderPath('Startup')) 'Phylactere.lnk'
# Toujours Windows PowerShell 5.1, même si ce script est lancé depuis PowerShell 7.
$PowerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

if ($Remove) {
    if (Test-Path $Shortcut) {
        Remove-Item $Shortcut
        Write-Host 'Démarrage automatique retiré.' -ForegroundColor Green
    }
    else {
        Write-Host 'Le démarrage automatique n''était pas installé.'
    }
    Write-Host 'Un serveur déjà lancé continue de tourner : .\scripts\stop.ps1 pour l''arrêter.'
    exit 0
}

$Python = Join-Path $RootDir 'server\.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) {
    throw 'Le serveur n''est pas installé : lance d''abord .\scripts\install.ps1'
}
# Port et journal réels, lus dans la configuration (le port peut avoir été changé).
$Port, $LogFile = & $Python -c "from phylactere.__main__ import LOG_FILE; from phylactere.config import load_settings, log_dir; print(load_settings().server.port); print(log_dir() / LOG_FILE)"
if ($LASTEXITCODE -ne 0) {
    throw 'Lecture de la configuration impossible : lance .\scripts\start.ps1 pour voir l''erreur.'
}

$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($Shortcut)
$link.TargetPath = $PowerShellExe
$link.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$StartScript`""
$link.WorkingDirectory = $RootDir
$link.WindowStyle = 7  # réduite : évite l'éclair d'une fenêtre avant -WindowStyle Hidden
$link.Description = 'Serveur Phylactère (traduction des webtoons)'
$link.Save()
Write-Host "Raccourci créé : $Shortcut" -ForegroundColor Green
Write-Host 'Le serveur démarrera à chaque ouverture de session, sans fenêtre.'

# Démarre le serveur maintenant (s'il tourne déjà, la nouvelle instance s'arrête seule).
Start-Process -FilePath $Shortcut
Write-Host 'Démarrage du serveur…'
$ok = $false
for ($i = 0; $i -lt 30 -and -not $ok; $i++) {
    Start-Sleep -Seconds 1
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
        $ok = [bool]$health.ok
    }
    catch {
        # pas encore prêt
    }
}
if ($ok) {
    Write-Host "Serveur lancé : http://127.0.0.1:$Port/health" -ForegroundColor Green
}
else {
    Write-Host "Le serveur ne répond pas encore sur le port $Port. Voir le journal : $LogFile" -ForegroundColor Yellow
}
Write-Host "Journal : $LogFile"
Write-Host 'Arrêt : .\scripts\stop.ps1 ; retrait du démarrage automatique : .\scripts\install-autostart.ps1 -Remove'
