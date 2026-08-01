---
description: Run autonomous Myrmex frontier delegation for a bounded objective
agent: myrmex-orchestrator
---

Use the `myrmex-frontier-delegation` skill in **autonomous** mode for this objective:

```text
$ARGUMENTS
```

Treat the objective as narrow unless the wording explicitly establishes a continuous parent objective. Do not push unless the user has already given explicit push authorization in this conversation. Continue active frontier waits, local implementation, independent verification, and frontier validation until the relevant objective is complete, a genuine human blocker occurs, the user interrupts, or a hard tool failure is persisted.

For a resumed run, check the persisted execution policy before initiating or
resuming a Frontier exchange. A `direct-only` policy is authoritative: do not
use Frontier or change the route silently; report the explicit policy blocker.

Before each browser exchange, persist its stable request/task identity through
`myrmex-state frontier <run-id> start`; on resume run
`myrmex-state reconcile <run-id>` first and recover the existing exchange
rather than sending a duplicate request.
