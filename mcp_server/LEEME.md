# Servidor MCP de Resonance

Deja que Claude (Desktop o Code) use Resonance desde el chat: buscar trabajos, ver el avance
de la corrida, listar los mejores avisos y registrar una postulación. No tiene lógica propia:
cada tool es una llamada HTTP a la API local, así que una búsqueda pedida desde el chat pasa
por el mismo grafo que una lanzada desde la UI. Transporte **stdio**; nada sale a stdout
fuera del protocolo (el log va a stderr y a `RESONANCE_HOME/logs/mcp.log`).

Si la app no está corriendo, el MCP la lanza en `--headless` y espera hasta 60 s a que
publique `runtime.json`. Si no aparece, la tool responde con un error que apunta a los logs.

## Tools

| Tool | Qué hace |
|---|---|
| `estado()` | Resumen: fuentes activas por vía (navegador, API, ATS) y retiradas, proveedores LLM con key, advertencias, si corre algo |
| `buscar_trabajos(pedido)` | Interpreta el pedido, lanza la corrida y devuelve `run_id` sin esperar. Si ya corre algo, manual o del cron, responde sin llamar al LLM |
| `estado_corrida()` | Si sigue corriendo y, cuando la app lo informa, el progreso |
| `resultados(tier=1, limite=20)` | Avisos de un tier: título, empresa, score, ubicación, url (máx. 50) |
| `detalle_aviso(url)` | Razones del score, veredictos y requisitos con evidencia de un aviso |
| `registrar_postulacion(url, estado="postulado", nota="")` | Alta de postulación |
| `salud_fuentes()` | Fuentes sospechosas o sin rendimiento primero; las sanas, en conteo |
| `abrir_panel()` | Abre la UI en ventana de app |

## Configurar el cliente

**Desarrollo** (sin empaquetar). `cwd` tiene que ser la raíz del repo: el módulo importa
`desktop.runtime` de ahí.

Claude Desktop (`claude_desktop_config.json`); en Linux el `command` es `<repo>/.venv/bin/python` y el `cwd` la ruta del repo:

```json
{
  "mcpServers": {
    "resonance": {
      "command": "C:\\ruta\\al\\repo\\.venv\\Scripts\\python.exe",
      "args": ["-m", "mcp_server"],
      "cwd": "C:\\ruta\\al\\repo"
    }
  }
}
```

Claude Code (`.mcp.json` en la raíz del repo, o `claude mcp add`); en Linux el `command` es `.venv/bin/python`,
y desde el directorio del repo `claude mcp add resonance -- <repo>/.venv/bin/python -m mcp_server` registra lo mismo:

```json
{
  "mcpServers": {
    "resonance": {
      "command": ".venv\\Scripts\\python.exe",
      "args": ["-m", "mcp_server"],
      "cwd": "."
    }
  }
}
```

**Empaquetado**: el instalador deja `resonance-mcp.exe` junto a `Resonance.exe`. No necesita
`cwd` ni Python instalado.

```json
{"mcpServers": {"resonance": {"command": "C:\\ruta\\resonance-mcp.exe"}}}
```

## Probarlo a mano

```
.venv\Scripts\python.exe -m pytest tests/test_mcp_server.py -q -p no:cacheprovider -o log_cli=false   # Windows
.venv/bin/python -m pytest tests/test_mcp_server.py -q -p no:cacheprovider -o log_cli=false             # Linux
```

Los tests usan una API falsa en localhost y un `desktop.runtime` falso; no necesitan la app.
