# Myrmex P1 Roadmap

## Colony Intelligence

### Portfolio scheduler

Priority: P2
Depends on: Campaign Intelligence
Constraints: Bounded concurrency

- Candidate coordination
Constraints: Read-only adapters

## Campaign Intelligence

### Deterministic planning

Priority: P1
Depends on: Storage Primitives, Contract Foundation
Constraints: Must preserve P0; Must be deterministic

- [x] Plan revision store
Constraints: Immutable revisions

- [ ] Backlog normalization
