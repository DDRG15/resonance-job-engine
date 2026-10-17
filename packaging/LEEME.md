# Empaquetado de Resonance (Windows)

Convierte el repo en una carpeta que arranca con doble clic y en un instalador. El
ejecutable **no contiene ningún dato ni secreto**: las keys, la base y las cookies viven en
`%LOCALAPPDATA%\Resonance` (`RESONANCE_HOME`) y sobreviven a actualizar o reinstalar.

## Qué produce

```
dist\Resonance\
    Resonance.exe        app de escritorio (API + ventana + bandeja), sin consola
    resonance-mcp.exe    servidor MCP por stdio, con consola
    _internal\           intérprete, dependencias y datos, compartidos por los dos .exe
dist\installer\Resonance-Setup-<versión>.exe     (solo si compilás el .iss)
```

Es PyInstaller **onedir**: arranca en segundos. `onefile` descomprimiría cientos de MB en
cada arranque y dispara más falsos positivos de antivirus. Los dos ejecutables comparten
un solo `_internal\`, así que el peso no se duplica.

Tamaño aproximado: ver el resumen que imprime `build.ps1` (el valor depende del build). Los navegadores de Playwright **no** van incluidos.

## Cómo construir

Requisitos: el venv del repo con las dependencias y `pyinstaller`, y `frontend\dist`
construido. El build **no corre npm**.

```powershell
cd frontend ; npm ci ; npm run build ; cd ..          # una vez, o cuando cambie el front
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

`build.ps1`:

1. Limpia `build\` y `dist\` de la raíz (nunca `frontend\dist`).
2. Falla con un mensaje claro si falta `frontend\dist\index.html`.
3. Corre PyInstaller con `packaging\resonance.spec`.
4. **Secretos**: abre `dist\` y falla si encuentra `.env`, `*.db`, `cookies\*.json`,
   `credentials\*.json` o el estado de runtime de la máquina de build (`user_profile.yaml`,
   `cv_profile.json`, `api_state.json`, `board_strategy.json`). La lista espeja
   `.dockerignore`; ver también `packaging\datos.py`.
5. **Smoke de la app**: `Resonance.exe --headless` con un `RESONANCE_HOME` temporal; espera
   `runtime.json` y `GET /api/status` 200 (tope 90 s); la termina.
6. **Smoke del MCP**: `resonance-mcp.exe` responde `initialize` por stdio.

Sale con código distinto de 0 si cualquier paso falla. `-SinSmoke` salta los pasos 5 y 6.

## Instalador (opcional)

Requiere [Inno Setup 6](https://jrsoftware.org/isinfo.php) (`ISCC.exe` en el PATH):

```powershell
ISCC.exe packaging\resonance.iss                 # versión 0.1.0 por defecto
ISCC.exe /DVersion=1.2.3 packaging\resonance.iss
```

Instala en `%LOCALAPPDATA%\Programs\Resonance` sin pedir administrador, con acceso directo
en el menú inicio, uno opcional en el escritorio y la casilla **Iniciar con Windows**
(clave `Run` de HKCU, ejecuta `Resonance.exe --headless`: el cron corre sin abrir ventana).
El desinstalador **conserva** `%LOCALAPPDATA%\Resonance` salvo que respondas «Sí» a la
pregunta final; en modo silencioso nunca lo borra.

## Navegador de Playwright

Chromium no viaja en el paquete (~150 MB; la mayoría de las fuentes son APIs y no abren
navegador). El ejecutable fija `PLAYWRIGHT_BROWSERS_PATH=%LOCALAPPDATA%\Resonance\browsers`
y se instala bajo demanda:

```powershell
resonance-mcp.exe --instalar-navegador       # muestra el avance en la consola
Resonance.exe --instalar-navegador           # sin consola: cuadro de aviso al terminar
```

El detalle queda en `%LOCALAPPDATA%\Resonance\logs\instalar-navegador.log`. Si definís
`PLAYWRIGHT_BROWSERS_PATH` vos mismo, se respeta.

## Registrar el MCP en Claude Desktop

Editá `%APPDATA%\Claude\claude_desktop_config.json` y agregá (con la ruta real de tu
instalación; si instalaste con el .iss, `%LOCALAPPDATA%\Programs\Resonance`):

```json
{
  "mcpServers": {
    "resonance": {
      "command": "C:\\Users\\<tu-usuario>\\AppData\\Local\\Programs\\Resonance\\resonance-mcp.exe"
    }
  }
}
```

Reiniciá Claude Desktop. Si la app no está corriendo, el MCP la arranca en segundo plano
(`Resonance.exe --headless`) y espera a que quede lista. Los logs: `logs\mcp.log` y
`logs\app.log` dentro de `RESONANCE_HOME`.

## SmartScreen y antivirus

Los ejecutables **no están firmados**. La primera vez, Windows SmartScreen muestra «Windows
protegió su PC»: **Más información → Ejecutar de todas formas**. Firmar exige un
certificado de código y queda fuera de alcance. Si un antivirus marca el paquete, el modo
onedir es el que menos falsos positivos genera; la alternativa es agregar una excepción
para la carpeta de instalación.

## Qué no entra nunca

`.env`, `*.db`, `cookies\*.json`, `credentials\*.json`, `backups\`, `tests\`, `docs\`, el
perfil del CV y el estado de runtime. Los datos propios que sí viajan son una lista cerrada
en `packaging\datos.py`: `frontend\dist`, `.env.example` y `user_profile.yaml.example`.
Todo lo que PyInstaller junta (también de terceros) pasa por el mismo filtro antes de
empaquetarse, y `tests\test_packaging.py` fija la lista.
