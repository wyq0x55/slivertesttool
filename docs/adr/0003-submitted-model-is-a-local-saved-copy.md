# A submitted model is a local saved copy

Opening a remote or external `.sil` in place does not stay runnable, and changing the registered version label would rewrite the identity of queued tasks. Each project model version is saved locally with the `.sil` and the dll, sbs, and pdb it loads. A task binds that saved version when it is queued. Registering another version adds a new copy and leaves queued tasks and existing run records unchanged. One worker process still runs concurrent tests up to the configured license limit.

## Consequences

Editing the files of a saved version can still affect queued tasks bound to that same version. A new version is a separate copy. Existing path-only rows are not backfilled with guessed copies.
