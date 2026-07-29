# Frontier response validation

Validate the `myrmex.frontier-exchange-result/v1` transport object first:

- `status=success`, `stable=true`;
- response is the newest assistant turn after the current outbound request;
- returned `request_id` matches exactly;
- no active generation;
- raw text is non-empty.

Then parse raw text using exact line/block rules, never substring matches over the page.

## Plan

A plan contains exactly one complete block:

```text
<proposed_plan>
request_id: <exact id>
...
</proposed_plan>
```

Require the current request/run/objective/base identity, repository-grounded findings, ordered decision-complete tasks, acceptance criteria, verification, allowed/protected scope, and no unrelated backlog or business invention. Reject and request correction when it conflicts with repository truth, dirty-tree safety, or capabilities.

## Result

Other responses begin:

```text
FRONTIER_RESULT
request_id: <exact id>
type: BLOCKING_CLARIFICATION | OBJECTIVE_COMPLETE | SUB_OBJECTIVE_COMPLETE | PARENT_OBJECTIVE_COMPLETE | OBJECTIVE_ALREADY_COMPLETE
```

Parse the exact `type:` line. Evaluate specific sub/parent tokens directly; never test whether the raw text merely contains `OBJECTIVE_COMPLETE`.

A repository-answerable clarification is handled by gathering safe evidence and sending a new request. A product/data/security/credential/destructive decision becomes a human blocker. Completion applies only to the named scope and requires evidence.
