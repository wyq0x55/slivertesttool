# Measure before performance changes

**Date**: 2026-10-13
**Status**: accepted
**Deciders**: project owner

## Context

Silver startup, writeback matching, comment lookups, and Excel reads already avoid their old full scans. Execution throughput stays capped by the license limit and one worker. The repository had no performance baseline. The two unmeasured growth paths are the Univer editor payload and dashboard history reads that load a project's run records into Python.

## Decision

A performance change needs a repeated local timing on a fixed synthetic dataset first. The baseline covers both candidate clocks separately: editor asset size plus the editor's paged sheet load, and the dashboard snapshot, trend, version breakdown, and version comparison. The dataset is 2,000 test items and 20,000 run records, with the editor page size of 500. The result is a local p50, not a CI failure gate. Public HTTP, database, import, and collaboration contracts stay unchanged. The Silver execution path stays unchanged until a measured queue wait exceeds license wait.

## Alternatives Considered

### Change the code first
- **Pros**: Removes an obvious cost immediately.
- **Cons**: Cannot prove the change is faster or that a later edit did not regress it.
- **Why not**: This repository does not accept an invented efficiency number.

### Optimize execution throughput first
- **Pros**: Queue time is user-visible.
- **Cons**: The wall clock is already bounded by the license limit.
- **Why not**: The pool and the single worker are accepted boundaries.

## Consequences

### Positive
- Later optimizations have a before-and-after number.
- Public behavior stays stable while the baseline is collected.

### Negative
- The baseline itself does not make the product faster.

### Risks
- A synthetic 2,000 by 20,000 dataset can point at the wrong clock if a real project is a different order of magnitude. Replace the dataset size before treating a result as the project's target.
