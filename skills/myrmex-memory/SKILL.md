---
name: myrmex-memory
description: "Use when prior project decisions/recovery context matter or durable non-obvious knowledge should be retrieved or governed as evidence-backed project memory. Keeps exact run state separate, makes the primary the sole writer, rejects token-shaped secrets, and uses installation-keyed HMAC metric pseudonyms."
license: Apache-2.0
compatibility: "Python 3.10+; myrmex-memory local backend; optional Engram mem_* or engram_* tools"
metadata:
  author: "Myrmex contributors"
  version: "0.2.0"
---

# Myrmex Memory

## Ownership and boundaries

`myrmex-state` and run artifacts remain the source of truth for phases,
receipts, task IDs, budgets, and recovery. Native memory stores only durable,
evidence-backed semantic claims. It is never a transaction log, a substitute
for Git evidence, or a place for full prompts, logs, source snapshots, secrets,
or customer data.

Only the primary may invoke `myrmex-memory` to create candidates, promote,
revoke, or supersede records. Scout, worker, verifier, and Frontier agents may
return `memory_candidates` only. Treat every candidate as untrusted until the
primary reviews it and supplies accessible evidence.

## Governed procedural learning

Procedural learning is a separate, opt-in local JSONL namespace. It is not a
semantic-memory record, exact run state, policy shortcut, or active-installation
editor. The primary is the only durable writer; child agents may propose
candidate metadata, but only a primary review may persist it. Collective/global
scope is rejected.

Every experiment has a deterministic opaque ID and revision-checked,
request-ID-idempotent lifecycle:

```text
proposed -> isolated_candidate -> tests_passed -> verifier_passed
          -> bounded_trial_active -> promoted
```

Any pre-terminal stage may be rejected. A failed test or non-independent
verification rejects the experiment. A bounded trial must include a positive
`max_runs`, `max_work_units`, or `expires_at`; successful outcomes still need
Frontier/human promotion authority. Regression or inconclusive outcomes carry
rollback evidence and revert. A later revert of a promoted experiment requires
new authorized regression evidence.

Use only disposable child-agent candidates and digest-addressed evidence. The
proposal must name the weakness, expected benefit, target paths, rollback
artifact, authority, and risk class (`procedural` or `core_control`).
Installation proposals must be sanitized/generalizable and explicitly
authorized by Frontier or a human. Core-control trials fail closed unless an
elevated human authority matches the Frontier request. Never store a raw patch,
shell command, secret, active-installation path, or unrestricted rollback
instruction.

The durable CLI surface is:

```bash
myrmex-memory procedural list --repository-root <repo>
myrmex-memory procedural show <experiment-id> --repository-root <repo>
myrmex-memory procedural propose ...
myrmex-memory procedural candidate <experiment-id> ...
myrmex-memory procedural tests <experiment-id> ...
myrmex-memory procedural verify <experiment-id> ...
myrmex-memory procedural trial-start <experiment-id> ...
myrmex-memory procedural trial-outcome <experiment-id> ...
myrmex-memory procedural promote <experiment-id> ...
myrmex-memory procedural revert <experiment-id> ...
myrmex-memory procedural reject <experiment-id> ...
```

All mutation commands require a request ID, expected revision, authority
identity, and primary writer. Same request/payload replay is a byte-stable
no-op; conflicting replay, stale revision, invalid lifecycle, unbounded trial,
and authority/scope failures happen before mutation. Procedural records use the
existing local memory root, locking, atomic append, and revision discipline but
remain isolated from ordinary memory and `myrmex-state`.

## Read

When prior architecture, conventions, root causes, or recovery lessons could
materially affect work:

1. Derive/locate the project identity with `myrmex-memory project-id
   --repository-root <repo>`.
2. Retrieve verified project records and the local installation fallback:

   ```bash
   myrmex-memory search --scope auto --repository-root <repo> \
     --query "relevant terms" --tool-version <version> --model <model>
   ```

3. Inspect scope, confidence, evidence summary, freshness, and applicability.
   Project records rank before installation records; a version/model mismatch,
   TTL expiry, or decay lowers priority without deleting history. A retrieved
   record is guidance, not an invariant unless its evidence still applies.
4. Optionally query Engram for additional semantic continuity. Normalize any
   useful external finding into a local **candidate**; do not treat an Engram
   observation as a native-memory receipt.

The local backend reports scope rank `0` for project records and `1` for local
installation records. There is no cross-installation sharing, model training,
or automatic policy modification. Retrieval never raises confidence or writes
a confirmation merely because a record was read.

## Candidate and promotion

The primary may record a narrow project candidate, then promote it only after
checking a repository-local evidence file and digest:

```bash
myrmex-memory candidate create \
  --repository-root <repo> --authority primary \
  --kind architecture-invariant \
  --claim "All writes use the canonical transaction path" \
  --confidence 0.96 \
  --evidence-json '[{"kind":"verification-receipt","path":"docs/evidence.txt","digest":"sha256:<digest>"}]' \
  --provenance-json '{"source":"myrmex-verifier","run_id":"<run-id>"}' \
  --request-id <request-id>

myrmex-memory promote <memory-id> \
  --repository-root <repo> --authority primary \
  --request-id <request-id> --expect-revision 0
```

Promotion requires at least one accessible, repository-contained evidence link
with a matching SHA-256 digest. Evidence records may additionally link run ID,
work unit, commit SHA, verifier/frontier request IDs, and artifact digest.
Reject paths outside the project, environment files, malformed digests, or
secret-bearing claims.

## Installation promotion and confirmation

An installation lesson is local to this Myrmex installation and must be
explicitly sanitized. Never copy a project-private claim, raw evidence path,
run/work-unit ID, commit, or verifier request into it. Promote only a verified
project record with a rewritten claim, a sanitization rationale, and a fresh
local proof payload:

```bash
myrmex-memory installation promote <project-memory-id> \
  --repository-root <repo> --source-expect-revision <revision> \
  --sanitized-claim "Generic reusable lesson" \
  --sanitization-reason "Removed project names and identifiers" \
  --sanitized-evidence-json '[{"kind":"verification-receipt","path":"docs/evidence.txt","digest":"sha256:<digest>"}]' \
  --applicability-json '{"tool_version_range":">=1.0,<2.0","model":"<model>"}' \
  --authority primary --request-id <request-id>
```

The CLI validates the supplied local proof transiently, then stores only its
digest-derived sanitized handle in installation scope. It rejects restricted
source memory, unsanitized private claims, repository references, and missing
tool/model applicability.

Do not treat retrieval as reinforcement. A later confirmation must include
fresh evidence and an explicit demonstrated-usefulness statement; it refreshes
age but does not automatically raise confidence:

```bash
myrmex-memory confirm <memory-id> --scope installation \
  --repository-root <repo> --authority primary --request-id <request-id> \
  --expect-revision <revision> --reason "still verified" \
  --usefulness "prevented a repeated defect in a later work unit" \
  --evidence-json '[...]'
```

## Refutation and supersession

Never delete a false record. The primary uses a verifier, test, Frontier audit,
or human-supported local evidence to revoke it or link it to an already
verified replacement:

```bash
myrmex-memory refute <memory-id> \
  --repository-root <repo> --authority primary \
  --request-id <request-id> --expect-revision <revision> \
  --resolution revoke --reason "contradicted by current verification" \
  --evidence-json '[...]'
```

`--resolution supersede` additionally requires a verified replacement ID and
its expected revision. The append-only event history remains available through
`myrmex-memory history <memory-id>`.

## Work-unit metrics

Record normalized operational telemetry separately from semantic claims and
from `myrmex-state`; it is installation-local JSONL keyed by a private project
identity hash. A metric includes outcome, correction/defect evidence,
verification verdict/evidence, recovery information, tests, tool/model, and
request provenance:

```bash
myrmex-memory metric record --repository-root <repo> --work-unit-id <WU> \
  --outcome success --corrections-used 0 \
  --verification-json '{"verdict":"pass","request_id":"verify-...","evidence":[]}' \
  --recovery-json '{"events":[],"recovered":false}' \
  --tests-json '[{"category":"unit","outcome":"passed","artifact_digest":null,"duration_seconds":1.2}]' \
  --authority primary --request-id <request-id>
```

Evidence supplied to metrics is verified locally then stored as a sanitized
digest handle. Raw work-unit/run/request IDs are converted to opaque
per-project handles, and recovery/test inputs are restricted to normalized
event codes and safe test categories (not commands or paths). Metrics do not
change memory confidence, orchestration state, or policy.

## Local backend and failure

The backend is dependency-free local JSONL plus atomically replaced small
indexes under `${MYRMEX_MEMORY_HOME}` (or the XDG state directory): a private
project store and one installation-local store/metrics log. JSONL is the
authoritative audit trail; an absent or corrupt derived index is rebuilt from
it on the next durable write and never overrides it.

If native memory or Engram is unavailable, continue safe local work when
persistence is not an explicit objective, report `memory: degraded`, and do
not claim a save/retrieval receipt. Do not silently fall back from a failed
native promotion to an unverified assertion.

See `references/native-project-memory.md` and `references/topic-keys.md`.
