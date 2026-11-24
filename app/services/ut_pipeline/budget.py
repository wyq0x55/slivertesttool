"""Calibrate the per-obligation step coefficient from delivered batches.

A batch's ceiling is ``coefficient x obligation count``.  Until now that
coefficient was whatever the operator guessed, because nothing read the
delivered batches back.  This module reads them: every finished batch already
froze its declared ceiling (``config_snapshot["function_step_limit"]``) and
every job froze the steps it consumed (its generation receipt) plus the
obligation count and exhaustion flag (its frozen witness handoff).  No new
collection is needed.

The result is advice, never an applied value.  Nothing here writes a batch, a
snapshot or a job; the suggestion is shown next to the binding form, and it
reaches a new batch only when an operator saves it there.  For the same
records the output is identical, which is why the samples are reported and
digested instead of only summarised.

Exhausted functions are counted but never averaged.  Their ``steps_used`` is a
truncated number -- the loop stopped at the ceiling -- so folding it into the
coefficient would drag the coefficient down and make the next suggestion
smaller than the evidence warrants.  They contribute only to the exhaustion
rate and the stop positions.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from datetime import datetime, timezone
from typing import Any, Iterable

from ...models import UtBatch, UtJob
from ..lanmatrix.errors import ServiceError

SCHEMA = "silver.ut-budget-suggestion/1"

# Headroom over the observed median.  The median describes the functions that
# already ran; a new function may be larger than the middle of the sample, so
# the suggestion keeps a margin before an operator has enough evidence to tune
# it.  Explicit and overridable: it is the one input the records cannot supply.
DEFAULT_SAFETY_FACTOR = 1.5

# Two pilot batches is what the proposal requires before a coefficient means
# anything; three functions keeps one outlying function from being the median.
MIN_BATCHES = 2
MIN_FUNCTIONS = 3

# Where a sample's ceiling came from: declared in the batch snapshot, or left
# empty so ut-agent's own default applied.
DECLARED = "declared"
DEFAULT = "default"
CEILING_SOURCES = (DECLARED, DEFAULT)

OK = "OK"
INSUFFICIENT_SAMPLES = "INSUFFICIENT_SAMPLES"

_EXHAUSTED_REASON = "GENERATION_BUDGET_EXHAUSTED"


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _positive_int(value: Any) -> int | None:
    """A strict positive int, or ``None`` for anything a counter should not hold.

    ``True`` is rejected as well: a bool in a counter column is a schema bug,
    not ``1``.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _steps(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _record(job: UtJob) -> dict[str, Any]:
    return _mapping(_mapping(job.witness).get("handoff"))


def _stop_position(record: dict[str, Any], generation: dict[str, Any]) -> str | None:
    """Where the function stopped, as the records wrote it.

    The handoff names the stop for triageable failures; a generation receipt
    names its own error.  ``None`` means the records did not say, which must
    stay distinguishable from an empty string.
    """
    for value in (record.get("error"), generation.get("error")):
        if isinstance(value, str) and value:
            return value
    return None


def _is_exhausted(record: dict[str, Any], generation: dict[str, Any]) -> bool:
    flag = _mapping(record.get("budget")).get("budget_exhausted")
    if isinstance(flag, bool):
        return flag
    # A record frozen before the flag existed: the receipt still names it.
    for value in (generation.get("error"), record.get("error"),
                  generation.get("status"), record.get("reason")):
        if isinstance(value, str) and value.startswith(_EXHAUSTED_REASON):
            return True
    return False


def _sample(batch: UtBatch, job: UtJob, declared: int | None,
            record: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """One ``batch x function`` sample, or the reason it cannot be one."""
    if not record:
        return None, "handoff_missing"
    generation = _mapping(job.generation)
    obligations = _positive_int(_mapping(record.get("obligations")).get("total"))
    if obligations is None:
        return None, "obligations_unknown"
    steps_used = _steps(generation.get("semantic_steps"))
    if steps_used is None:
        return None, "steps_unknown"
    exhausted = _is_exhausted(record, generation)
    return {
        "batch_id": batch.id,
        "function": getattr(getattr(job, "target", None), "function", "") or "",
        "ceiling": declared,
        "ceiling_source": DECLARED if declared is not None else DEFAULT,
        "step_limit": _positive_int(generation.get("step_limit")),
        "obligations": obligations,
        "steps_used": steps_used,
        "exhausted": exhausted,
        "stop_position": _stop_position(record, generation),
        "remaining_obligations": _steps(_mapping(record.get("obligations")).get("unresolved")),
        "derived_from_batch_id": batch.derived_from_batch_id,
    }, None


def _finished_batches(project_id: int) -> list[UtBatch]:
    """Batches whose outcome is final, oldest first.

    ``generating`` and ``running`` still move, so a sample taken from them
    would change without a new record being written.
    """
    return (UtBatch.query
            .filter(UtBatch.project_id == project_id,
                    ~UtBatch.status.in_((UtBatch.STATUS_GENERATING,
                                         UtBatch.STATUS_RUNNING)))
            .order_by(UtBatch.id).all())


def _jobs(batch: UtBatch) -> list[UtJob]:
    return UtJob.query.filter_by(batch_id=batch.id).order_by(UtJob.id).all()


def _collect(project_id: int, ceiling_sources: Iterable[str]) -> tuple[list[dict], dict]:
    sources = tuple(ceiling_sources)
    samples: list[dict[str, Any]] = []
    excluded: dict[str, int] = {}
    for batch in _finished_batches(project_id):
        declared = _positive_int(_mapping(batch.config_snapshot).get("function_step_limit"))
        for job in _jobs(batch):
            sample, reason = _sample(batch, job, declared, _record(job))
            if reason is not None:
                excluded[reason] = excluded.get(reason, 0) + 1
                continue
            if sample["ceiling_source"] not in sources:
                excluded["ceiling_source_filtered"] = excluded.get("ceiling_source_filtered", 0) + 1
                continue
            samples.append(sample)
    samples.sort(key=lambda item: (item["batch_id"], item["function"]))
    return samples, excluded


def _digest(samples: list[dict[str, Any]]) -> str:
    """A stable fingerprint of the sample set.

    Timestamps and the generation receipt's timings are not part of a sample,
    so re-reading the same finished batches always yields the same digest.
    """
    payload = json.dumps(samples, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _limit(value: float) -> int:
    """The ceiling a coefficient implies, never below the schema minimum."""
    return max(1, math.ceil(value))


def estimate(*, project_id: int, target_obligations: int | None = None,
             safety_factor: float = DEFAULT_SAFETY_FACTOR,
             ceiling_sources: Iterable[str] = CEILING_SOURCES) -> dict[str, Any]:
    """Suggest a per-function step ceiling for the project's next batch.

    Sampling unit is ``batch x function`` over the project's finished batches;
    only this project's records are read.  The coefficient is the median of
    ``steps_used / obligations`` over the non-exhausted samples -- the mean is
    dominated by one repair-heavy function and says nothing about the typical
    one.  The suggestion is ``ceil(median x target x safety_factor)`` and is
    advice only: nothing is written back, and a delivered batch's snapshot
    keeps the value it froze.

    ``target_obligations`` defaults to the median obligation count of the
    samples (the typical single function), so a caller that does not know the
    next batch's size still gets a per-function number.
    """
    if target_obligations is not None and (
            isinstance(target_obligations, bool)
            or not isinstance(target_obligations, int) or target_obligations < 1):
        raise ServiceError("目标义务数必须是正整数", code="BAD_REQUEST")
    if not (isinstance(safety_factor, (int, float))
            and not isinstance(safety_factor, bool)
            and math.isfinite(safety_factor) and safety_factor > 0):
        raise ServiceError("安全系数必须是正数", code="BAD_REQUEST")

    samples, excluded = _collect(project_id, ceiling_sources)
    used = [item for item in samples if not item["exhausted"]]
    exhausted = [item for item in samples if item["exhausted"]]
    batches = sorted({item["batch_id"] for item in used})
    digest = _digest(samples)

    missing: list[dict[str, Any]] = []
    if len(batches) < MIN_BATCHES:
        missing.append({"what": "batches", "have": len(batches), "need": MIN_BATCHES})
    if len(used) < MIN_FUNCTIONS:
        missing.append({"what": "functions", "have": len(used), "need": MIN_FUNCTIONS})

    document: dict[str, Any] = {
        "schema": SCHEMA,
        "state": INSUFFICIENT_SAMPLES if missing else OK,
        "project_id": project_id,
        "generated_at": _iso_now(),
        "samples": {
            "used": used,
            "used_count": len(used),
            "batches": batches,
            "exhausted_count": len(exhausted),
            "digest": digest,
        },
        "exhausted": {
            "rate": (round(len(exhausted) / len(samples), 6) if samples else None),
            "stop_positions": [item["stop_position"] for item in exhausted],
        },
        "coefficient": {
            "steps_per_obligation": None,
            "method": "median",
            "safety_factor": safety_factor,
        },
        "target": {"obligations": target_obligations,
                   "source": "requested" if target_obligations is not None else None},
        "suggested_step_limit": None,
        "excluded": {"count": sum(excluded.values()),
                     "reasons": dict(sorted(excluded.items()))},
        "missing": missing,
    }
    if missing:
        return document

    ratio = statistics.median(item["steps_used"] / item["obligations"] for item in used)
    target = target_obligations
    if target is None:
        target = max(1, round(statistics.median(item["obligations"] for item in used)))
        document["target"] = {"obligations": target, "source": "sample_median"}
    document["coefficient"]["steps_per_obligation"] = round(ratio, 6)
    document["suggested_step_limit"] = _limit(ratio * target * safety_factor)
    return document