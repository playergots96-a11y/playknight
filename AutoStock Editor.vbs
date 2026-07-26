' Launches AutoStock Editor without a console window (no build needed).
' Put this file next to main.py. Double-click to run.
' To get the icon: right-click this file > Create shortcut >
' shortcut Properties > Change Icon > browse to icon.ico.
'
' Finds the real pythonw.exe explicitly: the bare "pythonw" command can
' resolve to the Microsoft Store alias stub in WindowsApps, which exits
' silently and the app never appears.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("Wscript.Shell")
sh.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)

pyw = ""
base = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python"
If fso.FolderExists(base) Then
    For Each d In fso.GetFolder(base).SubFolders
        cand = d.Path & "\pythonw.exe"
        If fso.FileExists(cand) Then pyw = cand
    Next
End If
If pyw = "" Then pyw = "pythonw"    ' last resort: PATH lookup
sh.Run """" & pyw & """ main.py", 1, False
