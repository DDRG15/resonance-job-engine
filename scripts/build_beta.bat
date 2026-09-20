@echo off
REM ======================================================================REM build_beta.bat — Build and package GEMA for beta distribution
REM Run from the project root: scripts\build_beta.bat
REM Output: dist\gema-beta-v1\ (zip this folder and send to the friend)
REM =============================================================================

setlocal

set IMAGE_NAME=gema:beta-v1
set DIST_DIR=dist\gema-beta-v1

echo.
echo ============================================================
echo  GEMA Beta Builder
echo ============================================================
echo.

REM ── Supply-chain gate, Python (AUDIT #2) ─────────────────────────────────
REM A green test suite over a compromised dependency is a green light into an
REM incident. No beta image ships without a clean CVE scan of its pinned deps.
echo [1/7] Supply-chain gate (Python): scanning requirements for known CVEs...
python -m pip_audit -r requirements.txt --progress-spinner off
if %ERRORLEVEL% neq 0 (
    echo ERROR: pip-audit found known vulnerabilities in Python dependencies.
    echo        Fix or pin around the CVEs before shipping this image to a friend.
    pause
    exit /b 1
)
echo       Clean — no known CVEs.
echo.

REM ── Provider gate (v3) ────────────────────────────────────────────────────
REM Una key valida no significa que el modelo pinneado exista. El 2026-09-01 GEMA
REM shipeaba con llama-3.3-70b-versatile dado de baja: 404 en cada llamada, absorbido
REM en silencio por el fallback, y el sistema corriendo permanentemente degradado sin
REM que ninguna senal lo dijera. Un beta que sale asi le entrega al tester un sistema
REM que "funciona" con dos de siete proveedores muertos.
echo [2/7] Provider gate: verificando keys y pines de modelo...
python tools\check_provider_models.py
if %ERRORLEVEL% neq 0 (
    echo ERROR: hay credenciales vencidas o modelos dados de baja.
    echo        [KEY]  renovar la API key en .env
    echo        [GONE] cambiar el pin en config.py — correr con --list para ver opciones
    pause
    exit /b 1
)
echo.

REM ── Extraction quality gate (v3, opcional) ────────────────────────────────
REM Corre solo si hay corpus. Pega a proveedores reales, asi que no esta en pytest:
REM es lento, gasta cuota y no es determinista. Un cambio de prompt que mejora un
REM board y rompe otros tres no se nota hasta la corrida siguiente — esto lo detecta
REM antes de empaquetar.
REM OJO con la sintaxis: dentro de un bloque parentizado, cmd expande %ERRORLEVEL% al
REM PARSEAR el bloque, no al ejecutarlo — asi que el if interno leeria el valor VIEJO y
REM el gate nunca bloquearia. Reproducido: un comando que sale con 1 pasaba de largo.
REM `if errorlevel 1` lee el valor vivo y es la forma que funciona dentro de bloques.
echo [3/7] Extraction quality gate...
python tools\eval_extraction.py --dry-run >nul 2>&1
if errorlevel 1 goto :sin_corpus
python tools\eval_extraction.py --gate
if errorlevel 1 (
    echo ERROR: la calidad de extraccion empeoro respecto de la corrida previa.
    echo        Revisar el detalle por caso en la tabla eval_results.
    pause
    exit /b 1
)
goto :fin_eval_gate
:sin_corpus
echo       Sin corpus todavia — gate omitido. Ver tests\fixtures\extraction_corpus\README.md
:fin_eval_gate
=======
REM ── Supply-chain gate, frontend ──────────────────────────────────────────
REM The React bundle ships inside the image. A vulnerable npm dep is baked in
REM exactly like a vulnerable pip dep. Same gate, same standard: no ship on high.
echo [4/7] Supply-chain gate (frontend): npm audit on the locked deps...
pushd frontend
call npm ci --no-audit --no-fund
if %ERRORLEVEL% neq 0 (
    echo ERROR: npm ci failed — cannot verify the frontend lockfile.
    popd
    pause
    exit /b 1
)
call npm audit --audit-level=high
if %ERRORLEVEL% neq 0 (
    echo ERROR: npm audit found high/critical vulnerabilities in frontend deps.
    echo        Fix or pin around them before shipping this image to a friend.
    popd
    pause
    exit /b 1
)
popd
echo       Clean — no high/critical npm advisories.
echo.

REM ── Build Docker image ────────────────────────────────────────────────────
echo [5/7] Building Docker image %IMAGE_NAME%...
docker build -t %IMAGE_NAME% .
if %ERRORLEVEL% neq 0 (
    echo ERROR: Docker build failed.
    pause
    exit /b 1
)
echo       Done.
echo.

REM ── Create dist folder ───────────────────────────────────────────────────
echo [6/7] Creating dist folder...
if exist %DIST_DIR% rmdir /s /q %DIST_DIR%
mkdir %DIST_DIR%
mkdir %DIST_DIR%\cookies
REM cookies\session must exist so the rw bind mount has a host dir to bind to
mkdir %DIST_DIR%\cookies\session
echo       Done.
echo.

REM ── Export Docker image ──────────────────────────────────────────────────
echo [7/7] Exporting image to tar (this may take a few minutes)...
docker save -o %DIST_DIR%\gema-beta-v1.tar %IMAGE_NAME%
if %ERRORLEVEL% neq 0 (
    echo ERROR: docker save failed.
    pause
    exit /b 1
)
echo       Done.
echo.

REM ── Copy companion files ─────────────────────────────────────────────────
echo       Copying launcher, compose file, and guide...
copy scripts\start_gema.bat        %DIST_DIR%\start_gema.bat        >nul
copy scripts\docker-compose.beta.yml %DIST_DIR%\docker-compose.yml  >nul
copy BETA_GUIDE.md                 %DIST_DIR%\BETA_GUIDE.md          >nul

REM Create a blank .env (wizard will fill it on first run)
echo. > %DIST_DIR%\.env

echo       Done.
echo.
echo ============================================================
echo  Package ready: %DIST_DIR%\
echo.
echo  Contents:
echo    gema-beta-v1.tar   ^(Docker image — ~3GB^)
echo    docker-compose.yml
echo    start_gema.bat     ^(friend double-clicks this^)
echo    BETA_GUIDE.md
echo    .env               ^(blank — wizard fills it^)
echo    cookies\           ^(empty — friend adds their own^)
echo.
echo  SCENARIO A ^(pre-configured^):
echo    Open %DIST_DIR%\.env and paste the friend's API keys,
echo    then zip and send. Wizard will not appear on first run.
echo.
echo  SCENARIO B ^(self-setup^):
echo    Leave .env blank. Zip and send.
echo    Friend runs start_gema.bat and the wizard guides them.
echo ============================================================
echo.
pause
