# Project rules for Myrmex

- Preserve existing architecture and public contracts unless the objective explicitly changes them.
- Protect pre-existing dirty files.
- Use repository-native test/build commands.
- Never access production systems or secrets.
- Prefer DIRECT for bounded work; frontier and SDD are explicit opt-in routes.
- For delegated work, one Myrmex worker owns one work unit and a verifier independently checks non-trivial behavior changes.
