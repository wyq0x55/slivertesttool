"""Export the signed-off unit-test CSVs as one archive with their provenance.

Approval is the platform's last step; this hands the approved candidates on
without deciding where they go (writing them into a WinAMS unit tree is a
delivery decision outside the platform). Each function contributes the CSV of
its most recently approved job. Every file is re-hashed against the digest
recorded when its attempt was created, and one mismatch or missing file fails
the whole export, so the archive never carries a CSV that changed after it was
reviewed.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from ...extensions import db
from ...models import LMUser, UtAttempt, UtBatch, UtJob
from ...models.lanmatrix import _iso, _utcnow
from ..lanmatrix import audit
from ..lanmatrix.errors import ServiceError
from . import pipeline

_MANIFEST_COLUMNS = (
    "function", "source_rel", "unit_rel", "file", "job_id", "batch_id", "attempt_seq",
    "origin", "csv_sha256", "machine_verdict", "repair_rounds", "approved_by", "approved_at",
    "review_note", "test_rules_ref", "generator_commit", "superseded_by",
    "execution_backend", "evidence_source", "counts_as_target_evidence", "delivery_validated",
    "gate_code", "gate_reason", "open_gap_count", "expected_value_source", "stub_mode",
    "trace_status", "human_override", "gap_counts", "expected_value_lineage",
    "expected_value_lineage_complete", "baseline", "review_trace", "post_review_verdict",
)


def compare_batches(project_id: int, base_id: int, derived_id: int) -> dict[str, Any]:
    """Read-only, per-function view of what a derived batch changed.

    Nothing is stored: every figure comes from the two batches' frozen columns,
    so the comparison cannot drift from them and cannot rewrite either batch.
    A figure the source never recorded is ``None``, never a guessed zero.
    """
    base = _batch(project_id, base_id)
    derived = _batch(project_id, derived_id)
    derived_jobs = {job.target.function: job for job in _jobs(derived)}
    rows = [_compare_row(job, derived_jobs.get(job.target.function), base, derived)
            for job in _jobs(base)]
    return {
        "base": _batch_view(base),
        "derived": _batch_view(derived),
        "derived_from_batch_id": derived.derived_from_batch_id,
        "functions": rows,
    }


def _batch(project_id: int, batch_id: int) -> UtBatch:
    batch = UtBatch.query.filter_by(id=batch_id, project_id=project_id).one_or_none()
    if batch is None:
        raise ServiceError("批次不存在", code="NOT_FOUND")
    return batch


def _jobs(batch: UtBatch) -> list[UtJob]:
    return (UtJob.query.filter_by(batch_id=batch.id)
            .order_by(UtJob.id).all())


def _batch_view(batch: UtBatch) -> dict[str, Any]:
    snapshot = batch.config_snapshot or {}
    return {
        "id": batch.id,
        "status": batch.status,
        "function_step_limit": snapshot.get("function_step_limit"),
        "budget_stop_observations": snapshot.get("budget_stop_observations"),
        "generation_step_limit": (batch.provenance or {}).get("generation_step_limit"),
    }


def _compare_row(base_job: UtJob, derived_job: UtJob | None,
                 base: UtBatch, derived: UtBatch) -> dict[str, Any]:
    target = base_job.target
    return {
        "function": target.function,
        "source_rel": target.source_rel,
        "base": _side(base_job, base),
        "derived": _side(derived_job, derived) if derived_job is not None else None,
    }


def _side(job: UtJob, batch: UtBatch) -> dict[str, Any]:
    witness = job.witness if isinstance(job.witness, dict) else {}
    record = witness.get("handoff") if isinstance(witness.get("handoff"), dict) else {}
    budget = record.get("budget") if isinstance(record.get("budget"), dict) else {}
    obligations = record.get("obligations") if isinstance(record.get("obligations"), dict) else {}
    admissible = record.get("admissible") if isinstance(record.get("admissible"), dict) else {}
    confirm = witness.get("confirm") if isinstance(witness.get("confirm"), dict) else {}
    generation = job.generation if isinstance(job.generation, dict) else {}
    snapshot = batch.config_snapshot or {}
    return {
        "job_id": job.id,
        "state": job.state,
        "step_limit": snapshot.get("function_step_limit"),
        "budget_stop_observations": snapshot.get("budget_stop_observations"),
        "generation_step_limit": (batch.provenance or {}).get("generation_step_limit"),
        "handoff_state": record.get("state"),
        "obligations_total": obligations.get("total"),
        "obligations_completed": budget.get("completed"),
        "obligations_remaining": budget.get("remaining"),
        "admissible_count": (admissible.get("count") if isinstance(admissible.get("count"), int) else None),
        "by_cause": obligations.get("by_cause"),
        "stopped_at": record.get("error") or record.get("reason"),
        "resolved": _resolved(generation),
        "replay": {
            "verdict": job.last_verdict or None,
            "confirm_status": confirm.get("status"),
            "confirmed": confirm.get("confirmed"),
        },
    }


def _resolved(generation: dict) -> int | None:
    """Assertions the generated suite recorded, or ``None`` when it recorded none."""
    summary = generation.get("intent_summary")
    if isinstance(summary, dict) and isinstance(summary.get("assertions"), int):
        return summary["assertions"]
    return None


def _archive_path(source_rel: str, function: str) -> str:
    """``<source_rel>/<function>.csv`` with every segment kept inside the archive."""
    parts = [part for part in PurePosixPath(source_rel.replace("\\", "/")).parts
             if part not in ("", ".", "..", "/") and ":" not in part]
    name = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in function) or "function"
    return "/".join([*parts, f"{name}.csv"])


def approved_jobs(project_id: int, batch_id: Optional[int] = None) -> list[UtJob]:
    """The most recently approved job of each function (optionally within a batch)."""
    query = UtJob.query.filter(UtJob.project_id == project_id, UtJob.state == UtJob.APPROVED)
    if batch_id:
        query = query.filter(UtJob.batch_id == batch_id)
    latest: dict[int, UtJob] = {}
    for job in query.order_by(UtJob.id):
        latest[job.target_id] = job
    return sorted(latest.values(), key=lambda job: (job.target.row_no if job.target else 0, job.id))



def _approval_trace(job: UtJob, attempt: UtAttempt, config: dict) -> dict[str, Any]:
    """Facts the approval must not discard: why it gated, and where expectations came from."""
    trace = (job.generation or {}).get("review_trace")
    machine = (trace or {}).get("machine") or {}
    captured = (machine.get("attempt_id") == attempt.id
                and machine.get("csv_sha256") == attempt.csv_sha256)
    facts = machine if captured else pipeline.candidate_trace(job, attempt)
    return {
        **{key: facts[key] for key in (
            "gate_code", "gate_reason", "machine_verdict", "open_gap_count", "gap_counts",
            "expected_value_source", "expected_value_lineage", "expected_value_lineage_complete", "baseline")},
        "trace_status": "CAPTURED_BEFORE_REVIEW" if captured else "CURRENT_EVIDENCE_ONLY",
        "review_trace": trace if captured else None,
        "human_override": trace["human_override"] if captured else None,
        "post_review_verdict": job.last_verdict or "",
        "stub_mode": str((config or {}).get("stub_mode") or ""),
    }


def build_archive(project_id: int, user_id: int, batch_id: Optional[int] = None) -> tuple[bytes, str]:
    jobs = approved_jobs(project_id, batch_id)
    if not jobs:
        raise ServiceError("没有审核通过的作业可导出", code="NOT_FOUND")
    attempts = {item.id: item for item in UtAttempt.query.filter(
        UtAttempt.id.in_([job.current_attempt_id for job in jobs]))}
    batches = {item.id: item for item in UtBatch.query.filter(
        UtBatch.id.in_({job.batch_id for job in jobs}))}
    reviewers = {user.id: user for user in LMUser.query.filter(
        LMUser.id.in_({job.reviewed_by for job in jobs if job.reviewed_by}))}

    entries: list[tuple[str, bytes, dict[str, Any]]] = []
    problems = []
    for job in jobs:
        attempt = attempts.get(job.current_attempt_id)
        target = job.target
        name = target.function if target else f"job-{job.id}"
        data = None
        if attempt is None or not attempt.csv_path:
            problems.append(f"{name}：通过的版本没有 CSV")
        else:
            try:
                data = Path(attempt.csv_path).read_bytes()
            except OSError:
                problems.append(f"{name}：CSV 文件已丢失")
            else:
                if hashlib.sha256(data).hexdigest() != attempt.csv_sha256:
                    problems.append(f"{name}：CSV 在审核后被改动（摘要不一致）")
        if problems:
            continue
        provenance = (batches.get(job.batch_id).provenance or {}) if job.batch_id in batches else {}
        reviewer = reviewers.get(job.reviewed_by)
        config = (batches.get(job.batch_id).config_snapshot or {}) if job.batch_id in batches else {}
        path = _archive_path(target.source_rel if target else "", name)
        entries.append((path, data, {
            "function": name,
            "source_rel": target.source_rel if target else "",
            "unit_rel": target.unit_rel if target else "",
            "file": path,
            "job_id": job.id,
            "batch_id": job.batch_id,
            "attempt_seq": attempt.seq,
            "origin": attempt.origin,
            "csv_sha256": attempt.csv_sha256,
            "machine_verdict": job.last_verdict or "",
            "repair_rounds": job.repair_rounds,
            "approved_by": (reviewer.display_name or reviewer.username) if reviewer else "",
            "approved_at": _iso(job.reviewed_at),
            "review_note": job.review_note or "",
            "test_rules_ref": provenance.get("test_rules_ref") or "",
            "generator_commit": provenance.get("generator_commit") or "",
            "superseded_by": job.superseded_by,
            **_approval_trace(job, attempt, config),
        }))
        if (attempt.execution or {}).get("evidence_source") == "HOST_EXECUTION":
            entries[-1][2].update(
                execution_backend="clang", evidence_source="HOST_EXECUTION",
                counts_as_target_evidence=False, delivery_validated=False,
                host_coverage=(attempt.execution or {}).get("coverage"),
                host_evidence_note="Host observation comparison only; not target coverage acceptance")
    if problems:
        raise ServiceError("导出中止，以下版本无法证明与审核时一致：" + "；".join(problems[:10]),
                           code="INTEGRITY_ERROR")

    paths = [path for path, _, _ in entries]
    if len(set(paths)) != len(paths):
        raise ServiceError("导出中止：有函数映射到相同的文件路径", code="INTEGRITY_ERROR")
    created = _utcnow()
    manifest = {"contract": "silver-test-platform.ut-approved-export/1", "project_id": project_id,
                "batch_id": batch_id, "created_at": _iso(created), "created_by": user_id,
                "machine_verdict_note": "machine_verdict 是 ut-agent 判定；审核通过不改变它",
                "files": [meta for _, _, meta in entries]}
    table = io.StringIO()
    writer = csv.DictWriter(table, fieldnames=_MANIFEST_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows({key: json.dumps(value, ensure_ascii=False, sort_keys=True)
                     if isinstance(value, (dict, list)) else value
                     for key, value in meta.items()} for _, _, meta in entries)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, data, _ in entries:
            archive.writestr(path, data)
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        # utf-8-sig so Excel opens the Chinese and Japanese text correctly.
        archive.writestr("manifest.csv", table.getvalue().encode("utf-8-sig"))
    audit.record("ut.export.approved", actor_id=user_id, object_type="ut_export",
                 object_id=project_id, project_id=project_id,
                 new_value={"batch_id": batch_id, "files": len(entries)})
    db.session.commit()
    # Local time in the file name (it is read by people); the manifest keeps UTC.
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return buffer.getvalue(), f"ut-approved-p{project_id}{f'-b{batch_id}' if batch_id else ''}-{stamp}.zip"
