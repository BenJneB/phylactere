#Requires -Version 5.1
<#
.SYNOPSIS
    Arrête le serveur Phylactère, en particulier celui lancé sans fenêtre par le démarrage
    automatique.

.DESCRIPTION
    Termine les processus « python -m phylactere » de l'utilisateur.
    Exécution : powershell -ExecutionPolicy Bypass -File .\scripts\stop.ps1
#>
$ErrorActionPreference = 'Stop'

# Le python.exe de la venv est un lanceur qui démarre le vrai interpréteur dans un processus
# enfant : les deux portent la même ligne de commande et sont arrêtés ensemble.
$processes = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" |
    Where-Object { $_.CommandLine -match '\s-m\s+phylactere(\s|$)' -and $_.CommandLine -notmatch '--check-ocr' })

if ($processes.Count -eq 0) {
    Write-Host 'Aucun serveur Phylactère en cours.'
    exit 0
}
foreach ($process in $processes) {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
}
Write-Host "Serveur Phylactère arrêté ($($processes.Count) processus)." -ForegroundColor Green
