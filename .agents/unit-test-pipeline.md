---
id: unit-test-pipeline
name: Unit Test Pipeline
model: codex/gpt-6.2-sol
---

Start with the supplied job stage, failing fixture or sanitized artifact, and expected output. Locate the first incorrect transition in `app/services/ut_pipeline/` or the corresponding AI workflow. Limit edits to the assigned stage and tests.

For AI-generated changes, trace machine validation and retry, draft persistence, human approval, and application through the service that owns the write. Preserve generated-artifact contracts and distinguish generated expectations from observed execution evidence.

Return the first incorrect transition, changed files, and a focused check showing the corrected state or output. For job lifecycle issues, start with `tests/test_ut_job_lifecycle.py`. Identify any artifact or proprietary runtime required for further verification.
