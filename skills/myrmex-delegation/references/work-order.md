# Work order construction

Use schema `myrmex.work-order/v1` from the installed Myrmex contracts.

Required practical content:

- bounded objective and explicit non-goals;
- repository root, branch, and base SHA;
- allowed, forbidden, and pre-existing dirty paths;
- repository findings needed to avoid rediscovery;
- acceptance criteria observable at behavior level;
- exact skills to load when relevant;
- required or discoverable verification commands;
- Git policy: no commit and no push for the worker.

Do not overconstrain file paths when discovery is genuinely part of implementation. In that case provide allowed roots and require the worker to report every touched path.

A work order is invalid if it asks the worker to decide pricing, permissions, data semantics, legal content, destructive migrations, or public contract behavior that the user/repository has not settled.
