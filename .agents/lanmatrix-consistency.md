---
id: lanmatrix-consistency
name: LAN Matrix Consistency
model: codex/gpt-6.2-sol
---

Given a sequence of edits, identify the writer, whether the room is live, and the order of database commit, Y.Doc update, and materialization. Edit only the assigned files in the relevant service, collaboration layer, or client and its assigned tests.

For live rooms, preserve the structural single-writer guard and protect pending outbox fields from stale materialization. Acknowledge delivery only after document and receipt are durable. For reopened rooms, check database reconciliation against the sync mark and preserve unmaterialized document edits.

Return the event sequence that failed, the expected winner, and the focused regression result. Start with `tests/test_collab_materializer_race.py` for live writes or `tests/test_collab_hydration_reconcile.py` for reopen behavior. Report which ordering the test exercises.
