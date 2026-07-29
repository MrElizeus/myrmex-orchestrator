# Empieza aquí

Myrmex Orchestrator se instala como un ecosistema OpenCode independiente. La primera instalación no cambia tu agente predeterminado y no ejecuta una prueba live del navegador.

## Ruta recomendada: instalación por subagente

1. Descomprime `myrmex-orchestrator-v0.1.0-alpha.1.zip`.
2. Abre OpenCode en cualquier repositorio no crítico.
3. Entrega al agente instalador:
   - la ruta absoluta de la carpeta extraída;
   - el contenido de [`PROMPT-INSTALL-MYRMEX.md`](PROMPT-INSTALL-MYRMEX.md).
4. Mantén estos valores en la primera instalación:

```text
SET_DEFAULT_AGENT=false
PATCH_MISSING_MCP=true
RUN_LIVE_FRONTIER_TEST=false
```

5. Reinicia OpenCode, selecciona `myrmex-orchestrator` y ejecuta:

```text
/myrmex-doctor
```

6. Valida primero una tarea DIRECT pequeña y luego una DELEGATED acotada.
7. Para comprobar el transporte real al frontier, usa por separado [`PROMPT-LIVE-SMOKE-TEST.md`](PROMPT-LIVE-SMOKE-TEST.md) con un perfil de navegador ya autenticado.

## Ruta manual

```bash
cd /ruta/myrmex-orchestrator-v0.1.0-alpha.1
./scripts/run-tests.sh
./scripts/preflight.sh
./scripts/install.sh
./scripts/verify-install.sh
```

Después reinicia OpenCode y ejecuta `/myrmex-doctor`.

## Primeras órdenes útiles

```text
/myrmex-direct <tarea clara y acotada>
/myrmex-delegate <tarea que merece worker + verificación>
/myrmex-frontier-interactive <objetivo frontier acotado>
/myrmex-frontier <objetivo frontier autónomo>
/myrmex-status [run-id]
/myrmex-resume <run-id>
```

No actives `--set-default` ni autorices push hasta completar las pruebas iniciales descritas en [`docs/OPERATIONS.md`](docs/OPERATIONS.md).
