<#
.SYNOPSIS
  Construye Resonance (PyInstaller onedir) y comprueba que el resultado arranca y no lleva secretos.

.DESCRIPTION
  1. Limpia build\ y dist\ (los de PyInstaller, en la raiz; frontend\dist NO se toca).
  2. Exige frontend\dist\index.html (este script no corre npm).
  3. PyInstaller con packaging\resonance.spec -> dist\Resonance\{Resonance.exe, resonance-mcp.exe, _internal\}.
  4. SMOKE de la app: Resonance.exe --headless con RESONANCE_HOME temporal, espera runtime.json
     y GET /api/status 200 (tope 90 s), y la termina.
  5. SMOKE del MCP: resonance-mcp.exe responde `initialize` por stdio.
  6. Secretos: dist\ no contiene .env, *.db, cookies\*.json, credentials\*.json ni el estado
     de runtime de la maquina de build (lista espejo de .dockerignore).

  Exit code distinto de 0 si CUALQUIER paso falla.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File packaging\build.ps1
  powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -SinSmoke
#>
param(
    [switch]$SinSmoke,
    [int]$TopeArranqueSegundos = 90
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Raiz = Split-Path -Parent $PSScriptRoot
Set-Location $Raiz

function Fallar([string]$mensaje) {
    Write-Host ""
    Write-Host "BUILD FALLIDO: $mensaje" -ForegroundColor Red
    exit 1
}

# --- 0. Python y front ---------------------------------------------------------
$Py = Join-Path $Raiz ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { Fallar "no existe $Py (creá el venv: python -m venv .venv ; .venv\Scripts\pip install -r requirements.txt pyinstaller)" }

$ErrorActionPreference = 'Continue'   # un import fallido escribe a stderr; no debe cortar el script
& $Py -c "import PyInstaller" 2>$null
$okPyi = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
if ($okPyi -ne 0) { Fallar "PyInstaller no esta instalado en el venv (.venv\Scripts\pip install pyinstaller)" }

if (-not (Test-Path (Join-Path $Raiz "frontend\dist\index.html"))) {
    Fallar "falta frontend\dist\index.html. Corré: cd frontend ; npm ci ; npm run build"
}

# --- 1. Limpieza ---------------------------------------------------------------
foreach ($d in @("build", "dist")) {
    $ruta = Join-Path $Raiz $d
    if (Test-Path $ruta) { Remove-Item -Recurse -Force $ruta }
}

# --- 2. PyInstaller ------------------------------------------------------------
Write-Host "== PyInstaller ==" -ForegroundColor Cyan
$ErrorActionPreference = 'Continue'   # PyInstaller escribe INFO por stderr; no es un error
& $Py -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging\resonance.spec
$codigoPyi = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
if ($codigoPyi -ne 0) { Fallar "PyInstaller termino con codigo $codigoPyi" }

$Dist = Join-Path $Raiz "dist\Resonance"
$ExeApp = Join-Path $Dist "Resonance.exe"
$ExeMcp = Join-Path $Dist "resonance-mcp.exe"
foreach ($e in @($ExeApp, $ExeMcp)) {
    if (-not (Test-Path $e)) { Fallar "PyInstaller no produjo $e" }
}
if (-not (Test-Path (Join-Path $Dist "_internal\frontend\dist\index.html"))) {
    Fallar "dist no contiene _internal\frontend\dist\index.html (la UI no viajaria)"
}

# --- 3. Secretos: dist\ no puede llevar nada de la maquina de build -------------
function Buscar-Prohibidos([string]$carpeta) {
    $dirsVetados = '\\(cookies|credentials|backups|drift_quarantine)\\'
    $nombresVetados = @('.env', '.env.local', 'user_profile.yaml', 'cv_profile.json',
                        'api_state.json', 'board_strategy.json')
    Get-ChildItem -LiteralPath $carpeta -Recurse -Force -File | Where-Object {
        $n = $_.Name.ToLower(); $f = $_.FullName.ToLower()
        ($nombresVetados -contains $n) -or
        ($n -like '.env.*.local') -or
        ($n -like '*.db') -or ($n -like '*.db-shm') -or ($n -like '*.db-wal') -or ($n -like '*.bak') -or
        ($n -like '*.log') -or ($n -like '*.xlsx') -or
        ($f -match '\\cookies\\.*\.json$') -or ($f -match '\\cookies\\session\\') -or
        ($f -match '\\credentials\\.*\.(json|p12)$') -or
        ($f -match '\\(backups|drift_quarantine)\\')
    }
}

Write-Host "== Secretos en dist\ ==" -ForegroundColor Cyan
$hallados = @(Buscar-Prohibidos (Join-Path $Raiz "dist"))
if ($hallados.Count -gt 0) {
    $hallados | ForEach-Object { Write-Host "  PROHIBIDO: $($_.FullName)" -ForegroundColor Red }
    Fallar "dist\ contiene $($hallados.Count) archivo(s) que nunca deben ir en un artefacto"
}
Write-Host "  OK: sin .env, *.db, cookies\*.json, credentials\*.json ni estado de runtime"

# --- 4. Smoke -------------------------------------------------------------------
function Detener-Arbol([int]$procId) {
    $previo = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & taskkill /PID $procId /T /F 2>&1 | Out-Null
    $ErrorActionPreference = $previo
}

$tiempoArranque = $null
if (-not $SinSmoke) {
    [System.Net.WebRequest]::DefaultWebProxy = $null   # 127.0.0.1 nunca pasa por proxy
    $home_ = Join-Path ([System.IO.Path]::GetTempPath()) ("resonance-smoke-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
    New-Item -ItemType Directory -Path $home_ | Out-Null
    $envPrevio = $env:RESONANCE_HOME
    $env:RESONANCE_HOME = $home_
    $proc = $null
    try {
        # El primer arranque de un onedir recien escrito es el mas lento (el antivirus escanea
        # cada .pyd; con la maquina cargada se han medido de 7 s a mas de 90 s). Un exe que
        # MUERE falla al instante; uno que solo tarda mas que el tope tiene un segundo intento,
        # ya con los archivos escaneados.
        $ok = $false
        foreach ($intento in 1..2) {
            Write-Host "== Smoke: Resonance.exe --headless (intento $intento/2, RESONANCE_HOME=$home_) ==" -ForegroundColor Cyan
            Remove-Item -Force (Join-Path $home_ "runtime.json") -ErrorAction SilentlyContinue
            $reloj = [System.Diagnostics.Stopwatch]::StartNew()
            $proc = Start-Process -FilePath $ExeApp -ArgumentList "--headless" -PassThru -WindowStyle Hidden `
                                  -WorkingDirectory $Dist
            $rt = Join-Path $home_ "runtime.json"
            $ultimoError = "runtime.json no aparecio"
            $murio = $false
            while ($reloj.Elapsed.TotalSeconds -lt $TopeArranqueSegundos) {
                if ($proc.HasExited) { $ultimoError = "Resonance.exe termino solo (codigo $($proc.ExitCode))"; $murio = $true; break }
                if (Test-Path $rt) {
                    try {
                        $info = Get-Content -Raw -LiteralPath $rt | ConvertFrom-Json
                        $resp = Invoke-WebRequest -UseBasicParsing -Uri "$($info.url)/api/status" -TimeoutSec 5
                        if ($resp.StatusCode -eq 200) { $ok = $true; break }
                        $ultimoError = "GET /api/status dio $($resp.StatusCode)"
                    } catch { $ultimoError = "GET /api/status fallo: $($_.Exception.Message)" }
                }
                Start-Sleep -Milliseconds 500
            }
            $tiempoArranque = [math]::Round($reloj.Elapsed.TotalSeconds, 1)
            if ($ok) { break }
            if (-not $proc.HasExited) { Detener-Arbol $proc.Id; Start-Sleep -Seconds 2 }
            if ($murio) { break }
            Write-Host "  sin respuesta en $TopeArranqueSegundos s: $ultimoError" -ForegroundColor Yellow
        }
        if (-not $ok) {
            $log = Join-Path $home_ "logs\app.log"
            if (Test-Path $log) { Write-Host "--- app.log (final) ---"; Get-Content $log -Tail 40 }
            Fallar "smoke de la app: $ultimoError (tope $TopeArranqueSegundos s por intento)"
        }
        Write-Host "  OK: runtime.json + GET /api/status 200 en $tiempoArranque s ($($info.url))"

        # --- MCP: handshake por stdio (packaging\smoke_mcp.py: initialize -> result) ---
        Write-Host "== Smoke: resonance-mcp.exe (initialize por stdio) ==" -ForegroundColor Cyan
        $ErrorActionPreference = 'Continue'
        & $Py (Join-Path $PSScriptRoot "smoke_mcp.py") $ExeMcp 30
        $codigoMcp = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
        if ($codigoMcp -ne 0) { Fallar "smoke del MCP: initialize no obtuvo respuesta (codigo $codigoMcp)" }
    } finally {
        if ($proc -and -not $proc.HasExited) { Detener-Arbol $proc.Id }
        $env:RESONANCE_HOME = $envPrevio
        Start-Sleep -Milliseconds 500
        Remove-Item -Recurse -Force $home_ -ErrorAction SilentlyContinue
    }

    # El smoke no debe dejar nada en dist\ (la app corrio con su RESONANCE_HOME aparte).
    $hallados = @(Buscar-Prohibidos (Join-Path $Raiz "dist"))
    if ($hallados.Count -gt 0) {
        $hallados | ForEach-Object { Write-Host "  PROHIBIDO tras el smoke: $($_.FullName)" -ForegroundColor Red }
        Fallar "tras el smoke aparecieron archivos prohibidos en dist\"
    }
}

# --- 5. Resumen ----------------------------------------------------------------
$bytes = (Get-ChildItem -LiteralPath (Join-Path $Raiz "dist") -Recurse -Force -File | Measure-Object Length -Sum).Sum
$mb = [math]::Round($bytes / 1MB, 0)
Write-Host ""
Write-Host "BUILD OK" -ForegroundColor Green
Write-Host "  dist\Resonance\Resonance.exe      (app, sin consola)"
Write-Host "  dist\Resonance\resonance-mcp.exe  (MCP stdio)"
Write-Host "  tamano de dist\: $mb MB"
if ($tiempoArranque) { Write-Host "  arranque hasta /api/status 200: $tiempoArranque s" }
Write-Host "  siguiente paso (opcional): ISCC.exe packaging\resonance.iss  -> dist\installer\"
exit 0
