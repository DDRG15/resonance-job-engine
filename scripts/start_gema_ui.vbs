' F16 — GEMA standalone (el usuario, 2026-07-10): doble clic y listo.
' Levanta el server FastAPI OCULTO (sin ventana de consola) si no está
' corriendo, espera a que responda y abre la UI en el navegador.
' El cron (F8) se restaura solo al boot del server — GEMA queda autónomo
' mientras este proceso viva.

Option Explicit
Dim shell, fso, gemaDir, http, up, i

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
gemaDir = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))

' ¿Ya está corriendo? — probar el puerto antes de duplicar procesos
up = False
On Error Resume Next
Set http = CreateObject("MSXML2.XMLHTTP")
http.Open "GET", "http://localhost:8000/api/status", False
http.Send
If Err.Number = 0 And http.Status = 200 Then up = True
On Error GoTo 0

If Not up Then
    ' 0 = ventana oculta, False = no esperar
    shell.CurrentDirectory = gemaDir
    shell.Run "uvicorn api:app --port 8000 --workers 1", 0, False
    ' Esperar hasta 30 s a que el server responda
    For i = 1 To 30
        WScript.Sleep 1000
        On Error Resume Next
        Set http = CreateObject("MSXML2.XMLHTTP")
        http.Open "GET", "http://localhost:8000/api/status", False
        http.Send
        If Err.Number = 0 And http.Status = 200 Then Exit For
        On Error GoTo 0
    Next
End If

' F16 v2 (el usuario): ventana de APLICACIÓN, no pestaña de navegador.
' El modo --app abre una ventana standalone sin barra de URL ni pestañas,
' con su propia entrada en la barra de tareas. Edge viene con Windows;
' Chrome como alternativa; navegador normal como último recurso.
Dim edge, chrome
edge = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
If fso.FileExists(edge) Then
    shell.Run """" & edge & """ --app=http://localhost:8000", 1, False
ElseIf fso.FileExists(chrome) Then
    shell.Run """" & chrome & """ --app=http://localhost:8000", 1, False
Else
    shell.Run "http://localhost:8000", 1, False
End If
