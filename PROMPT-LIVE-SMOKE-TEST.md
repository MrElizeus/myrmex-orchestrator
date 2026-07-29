# Prompt — live Myrmex frontier smoke test

Use only after installation, OpenCode restart, `/myrmex-doctor`, and authentication of the designated browser profile. Run it in a non-critical repository. This test is authorized to send **one harmless frontier message**; it is not authorized to edit code, commit, push, enter credentials, or repair failures.

Replace the URL before use when no default conversation is already stored:

```text
FRONTIER_CHAT_URL=<existing designated frontier conversation URL>
```

---

You are validating the installed **Myrmex Orchestrator** frontier path end to end without changing repository content.

## Safety

- Work only in the current non-critical repository.
- Do not read `.env`, secrets, customer/production data, cookies, browser storage, or unrelated source.
- Use only the already authenticated browser profile and designated frontier conversation.
- Do not enter login, MFA, CAPTCHA, consent, payment, or credential data. A login/human-verification page is a `blocked` result.
- Do not edit, install, commit, push, or repair a failing package during this test.
- Send at most one new frontier request. Do not trigger failover unless the existing chat becomes unusable after the permitted single reload.

## Test procedure

1. Confirm the active primary agent is `myrmex-orchestrator`.
2. Run `/myrmex-doctor` behavior read-only and record PASS/WARN/FAIL for agents, skills, `myrmex-state`, memory, browser transport, and Git safety.
3. Inspect branch, HEAD, status, and protected dirty paths without mutation.
4. Confirm `myrmex-scout`, `myrmex-worker`, `myrmex-verifier`, and `myrmex-frontier` are available to `Task`; do not invoke the writer or verifier.
5. Confirm `myrmex-frontier-delegation` is available to this primary.
6. Initialize an **interactive, narrow, push-denied** local run with `myrmex-state`. Acquire its lock with a unique owner.
7. Build a minimal sanitized `myrmex.repository-context/v1` containing only evidence that `README.md` exists (or an equivalent harmless repository fact), the current Git identity, and protected dirty paths.
8. Render a request-scoped frontier prompt whose bounded objective is: **“Determine from the supplied evidence whether README.md exists; no code change is requested.”** The response may be `OBJECTIVE_ALREADY_COMPLETE`, a valid plan, or a blocking clarification, but it must use the exact unique request ID.
9. Persist the outbound artifact/digest and transition the local state to the correct pre-wait phase.
10. Delegate exactly one `send_and_wait` exchange to `myrmex-frontier` using `FRONTIER_CHAT_URL` or the already configured/stored designated chat.
11. Validate that the transport:
    - used browser transport rather than direct parent browser tools;
    - found the exact outbound request ID in the latest user turn;
    - actively waited rather than returning a mere waiting status;
    - extracted only the newest assistant turn;
    - observed stopped generation and identical text twice;
    - returned `myrmex.frontier-exchange-result/v1` with `status=success`, `stable=true`, the exact request ID, raw text, message ID, response type, poll count, and recovery observations;
    - did not classify markers from the outbound prompt or older messages.
12. Parse the raw frontier output using exact line/block rules. Do not implement any returned plan.
13. Save the response artifact/digest, update the run to `dormant` on success or `blocked` with the exact reason on failure, release the lock, and optionally save one compact memory test summary. Do not store polling heartbeats.
14. Run `myrmex-state show <run-id>` and confirm the final state and receipts are resumable and internally consistent.

## Report

Return a table or compact structured report with:

- active agent;
- run ID and final phase/status;
- agent/skill discovery;
- state CLI and lock behavior;
- memory availability/status;
- browser transport availability;
- outbound request ID and digest;
- returned message ID, response type, stability, and poll count;
- parser/result validation;
- evidence that no repository files changed;
- recovery actions, warnings, or blocker;
- overall verdict: `PASS`, `PASS_WITH_WARNINGS`, or `FAIL`.

Do not claim autonomous implementation/verification is proven by this transport-only smoke test. It proves the browser send/wait/extraction boundary and persisted frontier state.
