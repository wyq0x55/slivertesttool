# Project models are the only model source

A `.sil` model used to be resolvable from two places: a global registry and a
project's own model list, with a fallback order between them. The same test id
could therefore run against different models depending on admin state, and the
version stamped onto its run record could not be trusted. We removed the global
registry: `lm_project_models` is now the only source, and submitting against a
project with no model is refused instead of silently falling back.

## Consequences

A project that only ever relied on a global model must register one before it can
submit again. Model bundle directories are keyed by the project *code*, not its
name or row id, so renaming a project does not orphan its model files.
