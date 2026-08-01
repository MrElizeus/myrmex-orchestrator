# Native memory topics and relations

The native backend indexes project-local claim text and kind rather than using
an Engram topic key as its identity. Use a stable, narrow `kind` and a concrete
falsifiable claim, for example:

```text
architecture-invariant: All writes use the canonical transaction path.
repository-convention: New CLI state fields require a synchronized schema copy.
known-failure: A missing terminal result must be reconciled before another task starts.
```

Keep `project_identity` stable per repository. Record run/work-unit/commit and
verifier/frontier identifiers in **project** provenance/evidence when they
support a claim. Use `supersedes` and `superseded_by` for a stronger successor;
use `revoked_by` plus refutation evidence when no successor is valid.

Installation lessons are not Engram topics or project records. They must use a
generic, sanitized claim, null project identity/repository reference, a
tool/model applicability selector, and only digest-derived evidence handles.
Never copy a project topic name, path, run/WU ID, commit, or external request
ID into installation scope. Use `--scope auto` to retrieve the private project
record before an applicable local installation fallback.

Existing Engram topic keys remain optional adapter metadata, for example:

```text
myrmex/projects/<project>/architecture/<topic>
myrmex/frontier/<run-id>/summary
```

They never replace the native `memory_id`, local evidence validation, or audit
history.
