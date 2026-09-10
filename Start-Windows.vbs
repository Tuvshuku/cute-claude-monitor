' Native Windows launcher. No WSL is required.
Dim fso, shell, here, runner, windowsDir, powershell
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

here = fso.GetParentFolderName(WScript.ScriptFullName)
runner = fso.BuildPath(here, "run-windows.ps1")
windowsDir = shell.ExpandEnvironmentStrings("%SystemRoot%")
powershell = fso.BuildPath(windowsDir, "System32\WindowsPowerShell\v1.0\powershell.exe")

If Not fso.FileExists(runner) Then
    MsgBox "run-windows.ps1 not found next to this launcher:" & vbCrLf & runner, 16, "Claude Usage Bot"
    WScript.Quit 1
End If

shell.Run """" & powershell & """ -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & runner & """", 0, False
