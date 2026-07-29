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
