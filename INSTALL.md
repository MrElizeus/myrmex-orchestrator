# Guía de instalación

## 1. Requisitos

Recomendado:

- Linux (the supported baseline);
- Python 3.10+;
- OpenCode with global configuration in `~/.config/opencode` o `OPENCODE_CONFIG_DIR`;
- Node.js 18+ y `npx` para Playwright MCP;
- Engram disponible como `engram` para memoria persistente;
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

La suite incluye `check-package.py` y valida agentes, skills, comandos, JSON, helper JavaScript, scripts y una prueba funcional temporal de `myrmex-state`.

## 3. Instalación segura

```bash
./scripts/install.sh
```

El instalador crea un backup bajo:

```text
~/.config/opencode/backups/myrmex-orchestrator/<timestamp>/
```

Instala agentes, skills, comandos, docs/contratos y `~/.local/bin/myrmex-state`. Los MCP ya existentes no se reemplazan. Si faltan, crea entradas para Engram y Playwright.

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

Engram conserva conocimiento duradero, no cada heartbeat. Si Engram falla pero `myrmex-state` está sano, el flujo puede continuar con memoria semántica degradada cuando sea seguro.

## 8. Rollback

```bash
./scripts/uninstall.sh
```

Solo se eliminan archivos Myrmex que no hayan sido modificados después de instalarse. Los backups quedan disponibles para restauración manual.
