# Builds dashboard.py into a standalone Windows executable (MusicBotControlCenter.exe)
# using PyInstaller. Run from the music-bot folder:
#   powershell -ExecutionPolicy Bypass -File build_dashboard_exe.ps1
#
# The result is a single .exe in this folder that runs the dashboard without needing
# Python installed separately — still reads .env/cookies.txt/bot_output.log from
# wherever it's launched, so keep it in this folder (or copy the whole folder with it).

$ErrorActionPreference = "Stop"

& ".\venv\Scripts\python.exe" -m pip install --quiet --upgrade pyinstaller

& ".\venv\Scripts\python.exe" -m PyInstaller `
    --onefile `
    --windowed `
    --noconfirm `
    --name "MusicBotControlCenter" `
    --distpath "." `
    dashboard.py

Write-Host ""
Write-Host "Build complete: MusicBotControlCenter.exe" -ForegroundColor Green
