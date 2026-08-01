---
name: myrmex-git-delivery
description: "Use after verified work must be committed or pushed. Enforces separate authorization, intended staging, dirty-tree ownership, immutable verification, protected branches, and no force push."
license: Apache-2.0
metadata:
  author: "Myrmex contributors"
  version: "0.1.0"
---

# Myrmex Git Delivery

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

1. Before creating a PR, query the exact open `head`/`base` pair. Do not push again when the remote SHA already matches the intended candidate.
2. Record a typed `pull_request` operation intent before creating a draft PR, then immediately persist its number and URL in a run artifact/receipt. This intermediate state is `PR_CREATED_LABEL_PENDING`; reconcile the artifact through the operation observed-effect/receipt/confirmation lifecycle rather than a generic state patch.
3. Apply the label as a second step. If `gh pr edit --add-label` fails due to Projects scope, query the PR first and use the narrow issue-label REST endpoint as a fallback; do not request unrelated scopes.
4. Finalize the receipt as `PR_CREATED`, `PR_CREATION_FAILED`, or `LABEL_APPLICATION_FAILED`. Never retry creation until discovery proves the exact PR does not exist.

Use `scripts/github-pr-recovery.py` for this sequence. It never pushes branches and records a durable receipt before label application.
