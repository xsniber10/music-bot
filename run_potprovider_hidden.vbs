' Launches the bgutil PO Token provider with no visible console window.
' Used by the "BgutilPotProvider" Scheduled Task instead of invoking cmd.exe directly,
' since Task Scheduler shows a console window for interactive-session cmd actions.
Set objShell = CreateObject("WScript.Shell")
objShell.CurrentDirectory = "C:\Users\Madof\bgutil-ytdlp-pot-provider\server"
objShell.Run "cmd /c ""node build\main.js >> potprovider_output.log 2>&1""", 0, False
