; =============================================================================
; packaging/resonance.iss — instalador de Resonance (Inno Setup 6)
;
;   ISCC.exe packaging\resonance.iss
;   ISCC.exe /DVersion=1.2.3 packaging\resonance.iss
;
; Requiere dist\Resonance\ construido por packaging\build.ps1. Salida: dist\installer\.
;
; Reglas de este instalador:
;  - Sin admin: instala en %LOCALAPPDATA%\Programs\Resonance.
;  - El ejecutable no contiene datos del usuario. Los datos viven en
;    %LOCALAPPDATA%\Resonance (RESONANCE_HOME) y actualizar NO los toca.
;  - Desinstalar tampoco los borra, salvo que el usuario lo pida en la pregunta final.
; =============================================================================

#ifndef Version
  #define Version "0.1.0"
#endif

#define AppNombre "Resonance"
#define AppExe "Resonance.exe"
#define McpExe "resonance-mcp.exe"

[Setup]
; GUID fijo: identifica la app entre versiones para que actualizar sea sobrescribir.
AppId={{6D3F1B52-8C0A-4E7B-9A41-5B2C7E9D1F30}
AppName={#AppNombre}
AppVersion={#Version}
AppPublisher=DDRG15
DefaultDirName={localappdata}\Programs\{#AppNombre}
DefaultGroupName={#AppNombre}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist\installer
OutputBaseFilename=Resonance-Setup-{#Version}
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppNombre}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
DisableWelcomePage=no

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Tasks]
Name: "desktopicon"; Description: "Crear un acceso directo en el &escritorio"; GroupDescription: "Accesos directos:"; Flags: unchecked
Name: "autostart"; Description: "&Iniciar Resonance con Windows (la búsqueda automática corre en segundo plano, sin abrir ventana)"; GroupDescription: "Inicio:"; Flags: unchecked

[Files]
; Todo el onedir: Resonance.exe, resonance-mcp.exe y _internal\. Ningún dato del usuario.
Source: "..\dist\Resonance\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#AppNombre}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppNombre}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; Inicio con Windows: HKCU, sin admin. --headless = API y cron sin ventana ni bandeja.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#AppNombre}"; ValueData: """{app}\{#AppExe}"" --headless"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#AppExe}"; Description: "Abrir {#AppNombre}"; Flags: nowait postinstall skipifsilent

[Code]
// Los .exe en uso no se pueden sobrescribir ni borrar: se cierra la app y el MCP antes.
procedure CerrarResonance();
var
  Codigo: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, Codigo);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM {#McpExe}', '', SW_HIDE, ewWaitUntilTerminated, Codigo);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  CerrarResonance();
  Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  CerrarResonance();
  Result := True;
end;

// Los datos del usuario (.env con sus keys, la base de avisos, cookies) son suyos: por
// defecto se conservan. Desinstalar en modo silencioso NUNCA los borra.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Datos: String;
begin
  if CurUninstallStep <> usPostUninstall then Exit;
  if UninstallSilent then Exit;
  Datos := ExpandConstant('{localappdata}\Resonance');
  if not DirExists(Datos) then Exit;
  if MsgBox('¿Borrar también tus datos de Resonance?' + #13#10 + #13#10 + Datos + #13#10 + #13#10 +
            'Incluye tus claves de API, la base de avisos y postulaciones, y las cookies guardadas. ' +
            'Si vas a reinstalar, elegí No y todo sigue como lo dejaste.',
            mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    DelTree(Datos, True, True, True);
end;
