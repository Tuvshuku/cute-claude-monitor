' Native Windows launcher. No WSL is required.
Dim fso, shell, here, runner
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

here = fso.GetParentFolderName(WScript.ScriptFullName)
runner = fso.BuildPath(here, "run-windows.ps1")

If Not fso.FileExists(runner) Then
    MsgBox "run-windows.ps1 not found next to this launcher:" & vbCrLf & runner, 16, "Cute Claude Monitor"
    WScript.Quit 1
End If

shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & runner & """", 0, False

