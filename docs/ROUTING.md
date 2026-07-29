# Routing policy

## DIRECT

Prefer DIRECT when:

- objective and expected behavior are clear;
- repository context is local and coherent;
- no business/product decision is missing;
- change is reversible;
- risk to data, auth, billing, legal content, or public contracts is low;
- focused verification is available.

File count alone does not disqualify DIRECT.

## DELEGATED

Prefer DELEGATED when:

- broad mapping would inflate the primary context;
- the subsystem is unfamiliar;
- the change crosses coherent layers;
- implementation is lengthy;
- independent verification materially reduces risk;
- the user explicitly asks for delegation.

Normal delegated flow is at most one explorer, one worker, one verifier, and up to two bounded correction resumes.

## FRONTIER

Use only with explicit frontier intent. It is suitable for:

- architectural planning grounded in a real repository;
- long autonomous bounded objectives;
- a planning/validation loop where the frontier model is intentionally authoritative;
- continuation of a saved frontier session.

FRONTIER is not a reason to send the whole repository to the browser. Send a targeted context pack or use verified direct repository access.

## BLOCKED

Block only for material uncertainty that repository inspection cannot resolve, unavailable credentials/login, destructive production action, unsafe repository state, missing required capability, or an exhausted recovery budget.

Size is a soft limit: <=400 is normal, larger cohesive work needs a complete exception, and separable work is split.
