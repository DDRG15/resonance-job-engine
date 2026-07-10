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

shell.Run "http://localhost:8000", 1, False
