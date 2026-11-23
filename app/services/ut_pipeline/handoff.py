"""Freeze what a witness run can take as input from one generation.

ut-agent reports, per function, whether ``witness triage`` could accept any of
its unsolved obligations, and says so in the generation report rather than in
the per-unit receipt.  Silver freezes that answer onto the job so a person can
see, per function, whether there is triage input at all.

The record is observational.  It never changes a job's state, never gates
execution, and never becomes evidence: a function with no suite is reported as
``NO_SUITE`` (missing triage input) and one whose report could not be read as
``UNKNOWN``, so neither can be read as "nothing left to do".
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CONTRACT = "silver.ut-witness/1"
REPORT = "index-generation-report.json"

# ut-agent's own states, plus one silver adds for "the report did not say".
HANDOFF_READY = "HANDOFF_READY"
NO_ADMISSIBLE_OBLIGATION = "NO_ADMISSIBLE_OBLIGATION"
NO_SUITE = "NO_SUITE"
UNKNOWN = "UNKNOWN"
TOOL_STATES = (HANDOFF_READY, NO_ADMISSIBLE_OBLIGATION, NO_SUITE)


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _function(job) -> str:
    return getattr(getattr(job, "target", None), "function", "") or ""


def _unknown(reason: str, *, sha: str | None = None,
             run_status: str | None = None) -> dict[str, Any]:
    """A record for a function whose handoff could not be read.

    ``counts_as_target_evidence`` stays false: neither a report nor a triage
    run produces target evidence.
    """
    return {
        "state": UNKNOWN,
        "reason": reason,
        "budget": {"budget_exhausted": None, "total": None, "completed": None,
                   "remaining": None, "completed_is_solved": False},
        "obligations": {"total": None, "unresolved": None, "by_cause": {}},
        "admissible": {"count": None, "obligation_ids": [], "refused": [],
                       "unclassified": None},
        "mcdc": {"total": None, "pending_review": None, "pending_pair_ids": []},
        "ledger_confirmed": False,
        "source": REPORT,
        "report_sha256": sha,
        "report_run_status": run_status,
        "frozen_at": _iso_now(),
    }


def freeze(job, generation_dir: Path) -> dict[str, Any]:
    """Build the ``job.witness`` document for ``job`` from its generation.

    The report is read-only input.  A missing, unreadable, unfinished or
    functionless report yields ``UNKNOWN`` with the reason, never an exception:
    a handoff record may not turn a generated function into a failure.
    """
    path = Path(generation_dir) / REPORT
    if not path.is_file():
        return _document(_unknown("生成未写出 index-generation-report.json，无法判定分拣输入"))
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return _document(_unknown(f"生成报告不可读：{exc}"))
    sha = hashlib.sha256(raw).hexdigest()
    try:
        report = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _document(_unknown(f"生成报告不可解析：{type(exc).__name__}", sha=sha))
    if not isinstance(report, dict):
        return _document(_unknown("生成报告不是 JSON 对象", sha=sha))
    run_status = str(report.get("run_status") or "")
    if run_status != "COMPLETED":
        detail = str(report.get("error") or "").strip()
        return _document(_unknown(
            f"生成报告未完成（run_status={run_status or '空'}）"
            + (f"：{detail}" if detail else ""), sha=sha, run_status=run_status))
    handoff = report.get("triage_handoff")
    functions = handoff.get("functions") if isinstance(handoff, dict) else None
    record = functions.get(_function(job)) if isinstance(functions, dict) else None
    if not isinstance(record, dict):
        return _document(_unknown("生成报告未包含该函数的分拣记录", sha=sha,
                                  run_status=run_status))
    state = str(record.get("state") or "")
    if state not in TOOL_STATES:
        return _document(_unknown(f"生成报告的分拣状态不可识别：{state or '空'}",
                                  sha=sha, run_status=run_status))
    frozen = dict(record)
    frozen.update({
        "source": REPORT,
        "report_sha256": sha,
        "report_run_status": run_status,
        "frozen_at": _iso_now(),
    })
    return _document(frozen)


def _document(handoff: dict[str, Any]) -> dict[str, Any]:
    """The full per-job document; later phases fill triage, proposal and confirm."""
    return {"contract": CONTRACT, "handoff": handoff,
            "triage": None, "proposal": None, "confirm": None}


def document(job) -> dict[str, Any]:
    """The job's witness document, or an empty UNKNOWN one when never frozen."""
    stored = getattr(job, "witness", None)
    if isinstance(stored, dict) and isinstance(stored.get("handoff"), dict):
        return stored
    return _document(_unknown("尚未冻结分拣记录"))


def handoff(job) -> dict[str, Any]:
    return document(job)["handoff"]


def state(job) -> str:
    return str(handoff(job).get("state") or UNKNOWN)


def ready(job) -> bool:
    """Whether a witness run may take this job as input."""
    return state(job) == HANDOFF_READY


def missing_input(job) -> bool:
    """No suite at all: the job cannot even be triaged."""
    return state(job) == NO_SUITE


def unread(job) -> bool:
    """The report never said — neither a suite nor a proven absence of one."""
    return state(job) == UNKNOWN


def summary(job) -> dict[str, Any]:
    """The compact facts the job page shows, without the obligation-id lists."""
    record = handoff(job)
    budget = record.get("budget") or {}
    obligations = record.get("obligations") or {}
    admissible = record.get("admissible") or {}
    return {
        "state": record.get("state") or UNKNOWN,
        "reason": record.get("reason"),
        "error": record.get("error"),
        "budget_exhausted": budget.get("budget_exhausted"),
        "step_limit": budget.get("total"),
        "steps_used": budget.get("completed"),
        "remaining": budget.get("remaining"),
        "obligations_total": obligations.get("total"),
        "obligations_unresolved": obligations.get("unresolved"),
        "by_cause": obligations.get("by_cause") or {},
        "admissible_count": admissible.get("count"),
        "unclassified": admissible.get("unclassified"),
        "mcdc_pending": (record.get("mcdc") or {}).get("pending_review"),
        "source": record.get("source"),
        "report_sha256": record.get("report_sha256"),
        "frozen_at": record.get("frozen_at"),
    }