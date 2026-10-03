---
name: silver-orchestrator
description: Orchestrate non-trivial Silver Test Platform coding work with a high-quality root, DeepSeek execution subagents, and an independent Astra reviewer. Use for cross-component work, repository exploration, debugging, parallel workstreams, or changes where independent verification materially helps. Do not use for trivial localized edits.
---

# Silver Test Platform Orchestrator

`AGENTS.md` is authoritative. This skill schedules work; it does not weaken any
repository contract.

## Delegation gate

Use root-only execution for a genuinely small, already-understood localized
change. Delegate when repository exploration is required, the change crosses
ownership boundaries, debugging spans components, two independent workstreams
exist, external/version-specific facts matter, or independent review materially
reduces risk.

Do not spawn roles mechanically. Use only the roles that improve the task.

## Roles and routing

- `explorer`: read-only path/ownership tracing.
- `worker`: one bounded implementation surface.
- `tester`: targeted reproduction and verification.
- `researcher`: current external/version-specific facts.
- `reviewer`: independent post-change code review.

Routine execution roles use the repository-configured DeepSeek model. The
reviewer intentionally uses Astra. The root keeps the model selected by the
current Codex session and owns architecture, decomposition, integration, and the
final claim.

Useful repository decomposition:

- HTTP/auth/request translation -> routes
- workflows/business rules -> services
- persistence/query behavior -> models/PostgreSQL
- Synopsys runtime/process behavior -> runners/worker
- collaborative editing/materialization -> collab
- browser/editor behavior -> frontend

Prefer one writer per subsystem or file set.

## Delegation contract

Every delegated task must state:

1. Objective: one concrete outcome.
2. Scope: exact subsystem/files/question when known.
3. Context: only facts needed for the task.
4. Constraints: contracts that must not change.
5. Deliverable: evidence or implementation expected.
6. Acceptance criteria: how the root will verify success.

Exploration/research/review are read-only. A worker must stop and report when the
task expands into an architectural decision, schema/API break, new dependency,
security-sensitive design, or another worker's ownership.

## Parallelism and synchronization

Spawn independent read-only investigations together. Parallelize writers only
when their ownership surfaces do not overlap. Do not repeatedly poll agents.
Continue root integration work while independent lanes run, and collect results
only when they are needed for the next decision or final synthesis.

## Default non-trivial flow

1. Explore only the unclear ownership/path.
2. Root chooses the implementation direction.
3. Delegate bounded implementation surfaces.
4. Run targeted verification for changed behavior.
5. Use the independent reviewer when the diff is non-obvious, cross-component,
   security/data/concurrency-sensitive, or otherwise high-risk.
6. Root inspects the final diff and verifies material subagent claims.
7. Rely on repository CI for the full reproducible integration gate; keep real
   Silver/license behavior explicitly outside claims not validated internally.

Keep reports concise: conclusions, exact paths/symbols, commands/results, and
remaining risks rather than large raw logs.
