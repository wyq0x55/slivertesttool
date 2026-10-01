# AI authoring browser acceptance — October 2, 2026

## Verified scope

At engineering source `0cba408`, an isolated headless Edge browser exercised the
real production Univer grid, static editor/draft code, Flask HTTP endpoints and
PostgreSQL services. The primary run passed **43 assertions across 14 steps**;
candidate/review acceptance passed **21 assertions across 7 steps**. Neither
run had failed or blocked steps, or JavaScript page errors.

The primary flow covers native persisted-row selection, draft creation/linking,
inline invalid/valid edits, atomic partial human approval, document provenance,
candidate SBS and library forms, cancellation/retry, delayed response/navigation
fences and rejection of failure analysis without archived evidence.

The second flow covers human SBS approval, persisted non-current revisions and
unchanged active SBS bytes; library creation from reviewed saved steps and
human approval; stale edit/retry response fencing; and provider-error controls.
Both final database probes reported **0 tasks, 0 run records and 0 execution
endpoint requests**. Approving authoring assets did not launch a test.

## Containment and evidence

- Dedicated PostgreSQL 18 cluster on port 55432; final browser database
  `stp_ai_browser_20261002`, separate from all pytest databases.
- Fresh isolated browser context and temporary profile; no user browser profile,
  credentials, project database or proprietary Silver asset is used.
- Saved fixture model has inert SBS text and a deliberately absent SIL file.
  Provider replies are deterministic and explicitly synthetic.
- Browser HTTP is confined to the owned loopback server. The Python harness
  denies external connections and writes outside `.cache/ai-browser/`.
- Raw assertions, HTTP bodies, database probes, screenshots and asset hashes
  remain in the owned worktree's `.cache/ai-browser/`. All three tested UI asset
  hashes are unchanged between the start and finish of each acceptance run.

Commands actually executed:

```text
node .cache/ai-browser/acceptance.cjs
node .cache/ai-browser/acceptance.cjs --candidates
```

## Diagnosed and resolved

Initial production-grid acceptance failed because native row/cell gestures
produced `sheet.operation.set-selections` commands but no facade
`SelectionChanged` callback. The actual selection command is now consumed by
the adapter, with workbook and worksheet identity checks. Two valid RED unit
regressions reproduced lost/uncleared selection; four selection tests are GREEN.
The refreshed production Univer artifact is included. Pointer/keyboard gestures
then produced the real stored row IDs and enabled the AI matrix button.

The initial harness also assumed unwrapped SBS HTTP envelopes and an obsolete
"waiting cancellation" state. Those assertions were corrected to the inspected
HTTP contract and generation state machine, not worked around in production.
The final run used a fresh disposable database. Earlier built-in-grid fallback
results are not substituted for final production-Univer acceptance.

## Explicit limits

This is software/UI acceptance, not real Silver, license occupancy or pilot
efficiency evidence. The seeded active cancellation has no in-flight external
provider request; backend regressions prove late-result fencing and occupied
slot semantics separately. Optional live collaboration transport is not started
by this harness; PostgreSQL/pycrdt integration tests prove collaborative-safe
writeback and approval ownership, rather than a live multi-browser room claim.

The real two-module/20-approved-viewpoint pilot and manual timing remain
human-owned and unrecorded.
