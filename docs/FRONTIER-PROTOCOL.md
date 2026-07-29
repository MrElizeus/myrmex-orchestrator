# Frontier protocol

## Objective lifecycle

```text
context -> plan -> implement -> verify -> frontier validate
                         ^                 |
                         |--- correction --|
```

For continuous objectives, completion of one sub-objective triggers exactly one parent-objective gate.

## Request identity

Every browser exchange has a unique request ID. Plans and completion messages must echo it. The transport ignores markers from user prompts, old assistant messages, or a response with the wrong ID.

## Repository grounding

A context pack includes:

- branch/base SHA and Git status;
- relevant files and symbols;
- observed architecture/conventions;
- current behavior and tests;
- data/public contracts;
- protected dirty paths;
- missing decisions;
- evidence citations.

Sensitive values are excluded.

## Evidence bundle

Validation receives:

- approved plan;
- worker result;
- verifier result;
- actual changed files/diff stat;
- commands and exit results;
- candidate SHA/working state;
- commit/push state.

The frontier cannot validate merely from “implemented successfully.”

## Autonomous behavior

Autonomous means Myrmex continues the current objective without approval between phases. It does not authorize new scope, credentials, destructive actions, product decisions, or Git push.

The browser subagent actively waits in the same task. Exact phase/request/task/delivery receipts live in `myrmex-state`; Engram holds compact semantic continuity. The primary never ends a turn in an unresolved `waiting-for-frontier` state.
