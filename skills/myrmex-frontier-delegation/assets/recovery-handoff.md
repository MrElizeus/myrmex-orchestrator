We are continuing an existing Myrmex frontier run because the previous browser conversation became unusable. This is a recovery handoff, not a new objective.

## Identity

- Run ID: `{{RUN_ID}}`
- Objective ID: `{{OBJECTIVE_ID}}`
- New request ID: `{{REQUEST_ID}}`
- Resume phase: `{{RESUME_PHASE}}`

## Objective

{{USER_OBJECTIVE}}

## Repository

- Repository: `{{REPOSITORY}}`
- Branch: `{{BRANCH}}`
- Base/current SHA: `{{CURRENT_SHA}}`

## Last valid frontier plan or decision

{{LAST_VALID_FRONTIER_OUTPUT}}

## Execution and verification state

{{EXECUTION_STATE}}

## Git and delivery state

{{GIT_STATE}}

Continue as the frontier planning and validation authority for this exact objective. Preserve real failures and blockers.

Return exactly one response for request ID `{{REQUEST_ID}}`:

- one `<proposed_plan>` block for remaining work within the same objective;
- `FRONTIER_RESULT` with `type: OBJECTIVE_COMPLETE`, `SUB_OBJECTIVE_COMPLETE`, or `PARENT_OBJECTIVE_COMPLETE` when supported by evidence;
- or `FRONTIER_RESULT` with `type: BLOCKING_CLARIFICATION` when a material human decision is required.

Do not propose unrelated work.
