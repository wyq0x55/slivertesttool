"""Unit-test pipeline: 実施一覧 → ut-agent generation → LLVM → check → LLM repair → human review.

* :mod:`.bridge`   — subprocess client for the ``ut-agent bridge`` JSON contract.
* :mod:`.repair`   — local-LLM revision of a failing TestCsv (draft only).
* :mod:`.pipeline` — project binding, 実施一覧 import, batches and the job
  state machine.

ut-agent owns every testing judgement (generation, format and test-policy
checks, LLVM execution evidence, coverage targets).  This package only
orchestrates, records provenance and routes each outcome to a human.
"""
