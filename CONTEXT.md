# Silver Test Platform

A web platform that runs Synopsys Silver test cases on an internal network: a
tester picks test ids from a project's Test Matrix, a worker executes them on
licensed Silver instances, and the verdict flows back onto the matrix row.

## Language

### Execution

**Task**:
One queued execution of a single test id, identified by `task_key`. Re-running
the same test id in the same project reuses that task row.
_Avoid_: Job; "run" as a noun for the row

**Run record**:
An immutable verdict row stamped with the model name and version that produced
it. Tasks are mutable working state; run records are history.
_Avoid_: Result

**Verdict**:
The judge's explicit outcome for a test id, parsed from `jdgrslt.log`.
_Avoid_: Status

**Status**:
The task lifecycle state: `queued`, `running`, `passed`, `failed`, `cancelled`.
A task can be `passed` while its verdict is `FAIL`.
_Avoid_: Result, outcome

**Outcome**:
The normalised analytics bucket (`pass|fail|error|untestable|cancelled`) that the
dashboard and review flow share.
_Avoid_: Verdict

### Silver runtime

**Pool**:
The set of pre-warmed, reusable Silver instances the worker keeps alive so a
queued test does not pay Silver start-up cost.
_Avoid_: Cache

**Borrow / return**:
Taking an idle pooled instance for one test, then giving it back.
_Avoid_: Acquire / release, which name the license gate instead

**License limit**:
The administrator's configured maximum number of concurrent Silver runs. It is
persistent operator configuration.
_Avoid_: Concurrency

**Drain**:
A transient state that blocks new license acquisitions so the pool can shrink to
zero for shutdown. It never changes the license limit.
_Avoid_: Shutdown limit

**Poison**:
Marking a pooled instance unusable after a cancel or crash, so the pool disposes
it and creates a clean replacement.

### Models and evidence

**Project model**:
A `.sil` model registered against exactly one project, carrying a version label.
It is the only model source; no global registry exists.
_Avoid_: Global model, default model

**Model version**:
The validated label stamped onto every run record the model produces, and the
grouping key for per-version comparisons.
_Avoid_: Revision

**Writeback**:
Copying a finished run's evidence (result, version, executor, execution date,
log) onto the matching Test Matrix row.
_Avoid_: Sync

**Live room**:
A project's collaboration session with at least one connected client. Only live
rooms claim queued writebacks.
_Avoid_: Session

### Test Matrix

**Item**:
One Test Matrix row in a named sheet (`test`, `lib`, `const`, `io`).
_Avoid_: Row, when the sheet matters

**Test id**:
The identity of a test case within a project. Unique per project.
_Avoid_: Case id, which is the storage column that usually holds it