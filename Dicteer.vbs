' Launch Dicteer (tray icon) with no console window.
' Logs and errors go to dictate.log next to this file.
Dim fso, shell, here, py, script
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
here = fso.GetParentFolderName(WScript.ScriptFullName)
py = here & "\.venv\Scripts\pythonw.exe"
script = here & "\tray.pyw"
shell.CurrentDirectory = here
shell.Run """" & py & """ """ & script & """", 0, False
