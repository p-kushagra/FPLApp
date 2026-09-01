' ==========================================================================
'  FPL Command Center - silent launcher
'
'  Runs launch_fpl.bat with no console window, so the desktop shortcut opens
'  the browser and nothing else. The batch file still writes its own progress
'  to the two minimised child windows and to data\daemon.log, so a failed
'  start is diagnosable rather than invisible.
'
'  Window style 0 = hidden. False = do not wait for the batch to finish, which
'  matters because the batch deliberately blocks until the server is healthy.
' ==========================================================================
Option Explicit

Dim shell, fso, scriptDir, target

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
target = scriptDir & "\launch_fpl.bat"

If Not fso.FileExists(target) Then
    MsgBox "Cannot find launch_fpl.bat next to this script." & vbCrLf & vbCrLf & _
           "Expected at:" & vbCrLf & target, _
           vbCritical, "FPL Command Center"
    WScript.Quit 1
End If

' Run from the project directory so relative paths (.venv, data\) resolve.
shell.CurrentDirectory = scriptDir
shell.Run """" & target & """", 0, False

Set fso = Nothing
Set shell = Nothing
