' Generic hidden process launcher, shared by every Scheduled Task that
' service_controller.ensure_scheduled_task() auto-creates for a registry
' entry that doesn't have one yet.
'
' Task Scheduler shows a console window for interactive-session cmd actions,
' so tasks invoke this instead (same trick as the existing per-bot
' run_bot_hidden.vbs / run_potprovider_hidden.vbs, just parametrized so one
' script covers every future bot instead of hand-writing a new one each time).
'
' Usage: wscript.exe //B hidden_launcher.vbs "<working_dir>" "<command_line>"
Set objShell = CreateObject("WScript.Shell")
Set args = WScript.Arguments
objShell.CurrentDirectory = args(0)
objShell.Run "cmd /c """ & args(1) & """", 0, False
