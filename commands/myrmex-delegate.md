---
description: Run one structured Myrmex delegated implementation and verification workflow
agent: myrmex-orchestrator
---

Use the `myrmex-delegation` skill for this objective:

```text
$ARGUMENTS
```

Use repository exploration only when it adds value, delegate exactly one bounded writer work unit to `myrmex-worker`, and use `myrmex-verifier` independently for non-trivial behavior. Do not commit or push without explicit authorization.

For a resumed run, inspect its persisted execution policy before starting any
Task. If it is `direct-only`, do not delegate or silently change routes; report
`BLOCKED_DELEGATION_FORBIDDEN_BY_EXECUTION_POLICY` instead.


For a new persisted run this command is an explicit routing choice; initialize
with `execution.requested_policy=delegated` rather than `unresolved`.
