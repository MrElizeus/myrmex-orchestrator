# Guía de instalación

## 1. Requisitos

Recomendado:

- Linux (the supported baseline);
- Python 3.10+;
- OpenCode with global configuration in `~/.config/opencode` o `OPENCODE_CONFIG_DIR`;
- Node.js 18+ y `npx` para Playwright MCP;
- Engram opcional para continuidad semántica adicional; la memoria nativa de
  proyecto e instalación funciona offline;
- perfil Chrome/Chromium ya autenticado en el servicio frontier.

Ejecuta:

```bash
./scripts/preflight.sh
```

Los warnings por capacidades opcionales no siempre son fatales. Config JSON inválido, directorio no escribible o colisiones inseguras sí deben detener la instalación.

## 2. Validar el paquete

```bash
./scripts/run-tests.sh
```

La suite incluye `check-package.py` y valida agentes, skills, comandos, JSON,
helper JavaScript, scripts y pruebas funcionales temporales de `myrmex-state` y
`myrmex-memory` (incluidas lecciones de instalación y métricas por WU).

## 3. Instalación segura

```bash
./scripts/install.sh
```

El instalador crea un backup bajo:

```text
~/.config/opencode/backups/myrmex-orchestrator/<timestamp>/
```

Instala agentes, skills, comandos, docs/contratos y los ejecutables
`~/.local/bin/myrmex-state` y `~/.local/bin/myrmex-memory`. Los MCP ya
existentes no se reemplazan. Si faltan, crea entradas para Engram y Playwright.

No cambia `default_agent` por defecto.

## 4. Opciones

Directorio de configuración distinto:

```bash
./scripts/install.sh --config-dir /ruta/opencode
```

Directorio de binarios distinto:

```bash
./scripts/install.sh --bin-dir /ruta/bin
```

No tocar MCP:

```bash
./scripts/install.sh --no-mcp
```

Mostrar cambios sin escribir:

```bash
./scripts/install.sh --dry-run
```

Establecer Myrmex como predeterminado:

```bash
./scripts/install.sh --set-default
```

Nota: el script escribe `default_agent` en `opencode.json`. Si una configuración posterior `opencode.jsonc` define otro valor, el instalador/verificador lo reportará y un agente de instalación deberá resolverlo con backup y edición mínima.

## 5. Verificación

```bash
./scripts/verify-install.sh
```

También puedes indicar rutas:

```bash
./scripts/verify-install.sh \
  --config-dir ~/.config/opencode \
  --bin-dir ~/.local/bin
```

Reinicia OpenCode y ejecuta:

```text
/myrmex-doctor
```

## 6. Preparar frontier

El instalador no introduce credenciales. El perfil Playwright debe estar autenticado previamente.

Una entrada típica —solo como ejemplo— usa un perfil persistente y una versión fijada del MCP:

```json
{
  "mcp": {
    "playwright": {
      "type": "local",
      "enabled": true,
      "command": [
        "npx",
        "-y",
        "@playwright/mcp@0.0.78",
        "--browser=chrome",
        "--user-data-dir=/home/USER/.config/opencode/myrmex-chrome-profile"
      ]
    }
  }
}
```

No ejecutes dos clientes Playwright contra el mismo `--user-data-dir` simultáneamente.

Prueba primero:

```text
/myrmex-frontier-interactive <objetivo acotado y no crítico>
```

Luego usa la prueba live descrita en `PROMPT-LIVE-SMOKE-TEST.md`.

## 7. Memoria y estado

Estado exacto:

```bash
myrmex-state doctor
myrmex-state list
myrmex-state show <run-id>
```

Memoria de proyecto nativa y offline:

```bash
myrmex-memory doctor
myrmex-memory search --repository-root <repo> --query "arquitectura relevante"
```

Solo el primary promueve candidatos con evidencia local verificable. Puede
promover una lección de proyecto a la instalación únicamente tras reescribirla
como claim sanitizado, declarar qué se eliminó y aportar una evidencia nueva;
la instalación conserva solo handles de digest y aplicabilidad de
herramienta/modelo. TTL/decay baja prioridad, no borra ni refuerza por lectura;
las métricas normalizadas por WU no sustituyen `myrmex-state`. Engram conserva
continuidad semántica adicional, no cada heartbeat. Si Engram o la memoria
nativa fallan pero `myrmex-state` está sano, el flujo puede continuar cuando
sea seguro y debe informar `memory: degraded` sin inventar un recibo.

## 8. Rollback

```bash
./scripts/uninstall.sh
```

Solo se eliminan archivos Myrmex que no hayan sido modificados después de instalarse. Los backups quedan disponibles para restauración manual.
