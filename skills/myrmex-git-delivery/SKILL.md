---
name: myrmex-git-delivery
description: "Use after verified work must be committed or pushed. Enforces separate authorization, intended staging, dirty-tree ownership, immutable verification, protected branches, and no force push."
license: Apache-2.0
metadata:
  author: "Myrmex contributors"
  version: "0.1.0"
---

# Myrmex Git Delivery

## Tracking issue before pull request

GitHub delivery is an ordered, resumable protocol. Resolve the read-only policy
with `scripts/resolve-delivery-policy.py` first. Before invoking `gh`, persist a
typed `tracking_issue` intent with `myrmex-state operation`; include the complete
resolved policy, its `policy_digest`, repository, objective ID, scope digest,
all effective policy fields, canonical approval marker, creation policy, body
file, and receipt file. State recomputes the side-effect-free resolver from the
persisted exact inputs and verifies the policy digest, decision, and run mode
before the intent can proceed. Then invoke
`scripts/github-tracking-issue-recovery.py`. Persist its artifact twice through
`operation observe` and `operation receipt`, and confirm the operation only when
the receipt is the canonical `ISSUE_APPROVED` or `ISSUE_REUSED` and its
URL/number/identity marker match the intent. Alias statuses such as `SUCCESS`
are rejected.

The recovery marker is stable and exact:
`<!-- myrmex:tracking objective_id=<objective-id> scope_digest=<sha256> -->`.
Exact-marker discovery is always used for resume; the optional approved
pre-marker fallback is controlled by the resolved `reuse_matching_approved`
policy. Discovery failure, ambiguity, a missing approval marker, or an
unconfirmed create is a blocker, never permission to retry blindly.

## Authorization

Commit and push are separate. Authorization for one never implies the other, deployment, release, force push, or protected-branch bypass.

## Commit gate

1. Confirm exact objective and valid verification evidence.
2. Re-check branch, HEAD, status, diff, and protected pre-existing paths.
3. Stage only objective-owned paths.
4. Review staged diff for secrets, generated junk, and unrelated changes.
5. Commit with a concise repository-consistent message.
6. Collect deterministic Git evidence and verify the receipt; record commit SHA only after the target remains unchanged.

## Push gate

Confirm exact commits, remote, branch, and explicit push authorization. Refuse force/history rewriting and unauthorized protected-branch delivery. Push normally and record the result. Never infer deployment or release.

If tracked content/path/mode changes after verification, invalidate the gate and re-verify.

## Recovering partially successful GitHub operations

GitHub commands are independent side effects. Never chain create, label, query, and push in one shell expression where a later failure can hide a successful earlier operation.

1. Require the confirmed approved tracking-issue operation before creating a PR intent. Generate the body with `myrmex-state delivery <run-id> pr-body`; it appends the exact persisted issue URL and a stable body marker. A PR intent must retain that tracking operation ID, issue number/URL/marker, body file, mandatory `body_digest`, and receipt file. State requires an exact canonical issue URL token or marker, rechecks the digest before effect/receipt recording and confirmation, and requires effect/receipt PR identities to match.
2. Before creating a PR, query the exact open `head`/`base` pair. Do not push again when the remote SHA already matches the intended candidate.
3. Record a typed `pull_request` operation intent before creating a draft PR, then immediately persist its number and URL in a run artifact/receipt. This intermediate state is `PR_CREATED_LABEL_PENDING`; reconcile the artifact through the operation observed-effect/receipt/confirmation lifecycle rather than a generic state patch.
4. Apply the label as a second step. If `gh pr edit --add-label` fails due to Projects scope, query the PR first and use the narrow issue-label REST endpoint as a fallback; do not request unrelated scopes.
5. Finalize the receipt as `PR_CREATED`, `PR_CREATION_FAILED`, or `LABEL_APPLICATION_FAILED`, then confirm only the canonical `PR_CREATED` receipt. Never retry creation until discovery proves the exact PR does not exist.

Use `scripts/github-pr-recovery.py` for this sequence. It never pushes branches and records a durable receipt before label application.
