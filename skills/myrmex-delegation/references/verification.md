# Verification evidence

The worker performs a mandatory self-review before returning a work result:
status, diff check, stat, numstat, scope, generated artifacts, acceptance coverage,
and the requested positive/negative checks. The verifier independently checks the
same candidate.

Changed lines are a soft 400-line limit. A larger cohesive change must include a
complete size exception with reason, cohesion, and review strategy. A separable
change is split into work units; tests, documentation, and safety checks are
never deleted just to reduce the count.
