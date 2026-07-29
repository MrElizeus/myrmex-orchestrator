The current sub-objective has been validated as complete. Perform one bounded completion check for the original continuous parent objective. Do not ask generically for more work and do not invent unrelated backlog.

## Identity

- Request ID: `{{REQUEST_ID}}`
- Run ID: `{{RUN_ID}}`
- Parent objective ID: `{{OBJECTIVE_ID}}`

## Parent objective

{{PARENT_OBJECTIVE}}

## Completed sub-objective

{{COMPLETED_SUB_OBJECTIVE}}

## Current repository/Git evidence

{{CURRENT_EVIDENCE}}

Return exactly one of:

### A. Another concrete task exists inside the same parent objective

<proposed_plan>
request_id: {{REQUEST_ID}}
run_id: {{RUN_ID}}
objective_id: {{OBJECTIVE_ID}}
base_sha: {{CURRENT_SHA}}

# Next bounded sub-objective

## Summary
[Concrete repository-grounded task within the parent objective.]

## Repository Findings
[Evidence.]

## Implementation Tasks
1. [Decision-complete task.]

## Acceptance Criteria
- [Observable criteria.]

## Verification
- [Checks.]

## Safety and Scope
- Allowed roots: [paths]
- Protected/forbidden paths: [paths]

## Assumptions
- [None or minimal assumptions.]

## Executor Report Requirements
- [Required evidence.]
</proposed_plan>

### B. Parent objective complete

FRONTIER_RESULT
request_id: {{REQUEST_ID}}
type: PARENT_OBJECTIVE_COMPLETE
reason: [why the broader objective is complete or sufficient]
evidence:
- [repository/execution evidence]

### C. Human decision required

FRONTIER_RESULT
request_id: {{REQUEST_ID}}
type: BLOCKING_CLARIFICATION
question: [one material question]
options:
- A: [option]
- B: [option]
recommended_default: [option and reason]
