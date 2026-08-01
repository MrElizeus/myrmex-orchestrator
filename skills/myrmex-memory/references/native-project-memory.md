# Native governed memory records

`myrmex.memory/v1` is the backend-independent semantic-memory contract. The
local backend has two intentionally separate scopes:

- `project`: evidence-backed claims private to one repository identity.
- `installation`: sanitized, local-to-this-installation operational lessons.

Neither scope is `myrmex-state`. Exact phases, locks, receipts, operation
intents, budgets, and recovery remain in the state CLI and artifacts.

## Project lifecycle

```text
subagent observation
  -> primary review
  -> project candidate
  -> local evidence validation
  -> verified project memory
  -> confirmation | refutation -> revoked | superseded
```

Evidence is relative to the project root and digest-addressed. The CLI checks
it during promotion, confirmation, and refutation. Run IDs and request IDs can
enrich private project provenance but never replace accessible proof. The
append-only JSONL log preserves lifecycle snapshots; `index.json` is a
recoverable read optimization only.

## Installation boundary

Installation records require `sensitivity=sanitized`, null project identity and
repository reference, and at least one tool-version range or model selector.
A project-private record moves across the boundary only via `installation
promote`, which requires a distinct `--sanitized-claim`, an auditable
sanitization rationale, and a newly supplied `--sanitized-evidence-json`.

The raw local evidence is checked before write. The installation record keeps
only a generic `sanitized-receipt` and its digest: no repository path, run/WU
ID, commit SHA, verifier/frontier request, or source project identity. Its
provenance may retain only an opaque project-memory ID. Restricted memory is
never promotable.

Search with `--scope auto` returns project records before installation records.
For installation records, a model/tool mismatch lowers applicability priority;
TTL, `half-life:<seconds>`, and `linear:<seconds>` decay lower freshness
priority without deletion. Retrieval itself never reinforces a record.

To refresh an installation lesson, the primary supplies new accessible proof
and `--usefulness`; the confirmation event is auditable, resets its age, and
does **not** increase confidence automatically. Refutation and supersession
remain append-only and remove the old verified record from trusted retrieval.

## Work-unit metrics

`myrmex.work-unit-metric/v1` is a separate installation-local JSONL stream.
It uses hashed project/work-unit/run/request handles and captures outcome,
corrections/defect evidence, verification, normalized recovery codes, safe
test categories/outcomes/digests/durations, and tool/model applicability. Raw
test names, commands, paths, and free-text recovery labels are rejected. It is
not a semantic claim, cannot modify memory confidence, and cannot mutate state
or policy. Supplied evidence is validated locally then reduced to the same
sanitized digest handle.

Every persistent memory or metric command requires `--authority primary` and a
stable `--request-id`; subagents return candidate content in structured results
but never invoke the backend. There is no cross-installation sharing, model
training, automatic procedural learning, or automatic target-file editing.
