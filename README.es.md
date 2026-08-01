# Myrmex Orchestrator

Myrmex es un ecosistema open source enfocado exclusivamente en OpenCode para
ejecutar, delegar, verificar y recuperar trabajo de ingeniería.

Estado: alpha (0.1.0-alpha.1). Probado en Linux con OpenCode 1.18+, Python
3.10+ y Bash. Node 18+ se necesita para las pruebas DOM/browser. La ruta
frontier live aún requiere validación manual.

Además de `myrmex-state` para estado exacto de runs, Myrmex incluye
`myrmex-memory`: memoria de proyecto local/offline con candidatos, evidencia
digest-addressed, promoción explícita y revocación/supersesión auditables; suma
lecciones de instalación **sanitizadas** con aplicabilidad por herramienta/modelo,
TTL/decay, confirmación y métricas normalizadas por WU separadas. Engram es un
adaptador semántico opcional y no sustituye el estado exacto. La información
privada de proyecto no se comparte entre instalaciones ni modifica políticas
automáticamente.

Consulta el README principal, INSTALL.md y CONTRIBUTING.md. Licencia Apache-2.0.
