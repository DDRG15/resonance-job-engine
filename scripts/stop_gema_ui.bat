@echo off
REM F16 — apaga el server GEMA que start_gema_ui.vbs dejó corriendo oculto.
echo Buscando el server GEMA (uvicorn en :8000)...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr :8000 ^| findstr LISTENING') do (
    echo Matando PID %%p
    taskkill /PID %%p /F
)
echo Listo.
pause
