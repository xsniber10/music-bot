' Launches the Discord music bot with no visible console window.
' Used by the "MusicDiscordBot" Scheduled Task instead of invoking cmd.exe directly,
' since Task Scheduler shows a console window for interactive-session cmd actions.
Set objShell = CreateObject("WScript.Shell")
objShell.CurrentDirectory = "C:\Users\Madof\Desktop\music-bot"
objShell.Run "cmd /c ""venv\Scripts\python.exe -u bot.py >> bot_output.log 2>&1""", 0, False
