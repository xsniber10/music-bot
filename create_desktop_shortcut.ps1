# Creates a Desktop shortcut that launches the dashboard with no console window
# (via pythonw.exe, the windowless Python interpreter). Run from the music-bot folder:
#   powershell -ExecutionPolicy Bypass -File create_desktop_shortcut.ps1

$ErrorActionPreference = "Stop"

$WshShell = New-Object -ComObject WScript.Shell
$shortcutPath = Join-Path ([Environment]::GetFolderPath("Desktop")) "Music Bot Control Center.lnk"
$shortcut = $WshShell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = Join-Path $PSScriptRoot "venv\Scripts\pythonw.exe"
$shortcut.Arguments = '"' + (Join-Path $PSScriptRoot "dashboard.py") + '"'
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.IconLocation = "shell32.dll,43"
$shortcut.Description = "Launch the Music Bot Control Center"
$shortcut.Save()

Write-Host "Shortcut created at: $shortcutPath" -ForegroundColor Green
