"""Pure, provenance-aware reduction of one explicitly selected pilot session."""

from __future__ import annotations

import math

from .pilot_contract import AuditSnapshot, PilotInput


def _validate_observations(pilot: PilotInput, snapshot: AuditSnapshot) -> None:
    selected = {item.item_id for item in pilot.viewpoints}
    draft_ids = [draft.draft_id for draft in snapshot.drafts]
    if len(set(draft_ids)) != len(draft_ids) or set(draft_ids) != set(pilot.draft_ids):
        raise ValueError("Audit observations do not match the requested drafts")
    expected = {(attempt.task_key, attempt.run_count): attempt.item_id for attempt in pilot.run_attempts}
    observed = {(attempt.task_key, attempt.run_count): attempt.item_id for attempt in snapshot.runs}
    if len(observed) != len(snapshot.runs) or observed != expected:
        raise ValueError("Audit observations do not match the requested task attempts")
    if any(attempt.approved_draft_id is not None and attempt.approved_draft_id not in pilot.draft_ids
           for attempt in snapshot.runs):
        raise ValueError("Task generation links must identify a requested draft")
    for draft in snapshot.drafts:
        for item_ids in (draft.generated_item_ids, draft.accepted_item_ids, draft.conversion_item_ids):
            if len(set(item_ids)) != len(item_ids) or not set(item_ids) <= selected:
                raise ValueError("Draft candidate identities are duplicate or outside the selection")
        generated = set(draft.generated_item_ids)
        if not set(draft.accepted_item_ids) <= generated or not set(draft.conversion_item_ids) <= generated:
            raise ValueError("Accepted and converted candidates must have been generated")
        if draft.accepted_item_ids and draft.status != "approved":
            raise ValueError("Unapproved drafts cannot have accepted candidates")


def build_report(pilot: PilotInput, snapshot: AuditSnapshot | None = None) -> dict:
    digest = pilot.selection_digest()
    if snapshot is not None and snapshot.selection_sha256 != digest:
        raise ValueError("Audit snapshot does not match the pilot input")
    verified_source = snapshot is not None and snapshot.source == "postgresql_readonly"
    timings = pilot.timings.model_dump(mode="json")
    missing = [name for name, value in timings.items() if value is None]
    metric_names = (
        "generated_candidates", "accepted_candidates", "conversion_success_candidates",
        "draft_acceptance_rate", "executable_conversion_success_rate",
        "real_authenticated_attempts", "real_completed_viewpoints", "synthetic_attempts",
    )
    metrics = dict.fromkeys(metric_names)
    readiness = {"checked": bool(verified_source), "ready": False, "issues": ["database_audit_missing"]}
    saved_minutes = None
    saved_percent = None
    if not verified_source:
        missing.append("database_audit")
    else:
        _validate_observations(pilot, snapshot)
        readiness = {"checked": True, "ready": not snapshot.preflight_issues,
                     "issues": list(snapshot.preflight_issues)}
        generated = {(draft.draft_id, item_id) for draft in snapshot.drafts for item_id in draft.generated_item_ids}
        accepted = {(draft.draft_id, item_id) for draft in snapshot.drafts for item_id in draft.accepted_item_ids}
        converted = {(draft.draft_id, item_id) for draft in snapshot.drafts for item_id in draft.conversion_item_ids}
        executable_accepted = set()
        executable_pairs = set()
        approved = set()
        for draft in snapshot.drafts:
            if draft.status == "approved" and not draft.issues:
                approved.update(draft.accepted_item_ids)
                if draft.provider_kind == "live":
                    eligible = set(draft.accepted_item_ids) & set(draft.conversion_item_ids)
                    executable_accepted.update(eligible)
                    executable_pairs.update((draft.draft_id, item_id) for item_id in eligible)
        authentic = [attempt for attempt in snapshot.runs if (
            attempt.verified and attempt.finalised and not attempt.issues
            and attempt.evidence_kind == "silver_runtime" and attempt.runner_backend == "silver"
            and attempt.status in {"passed", "failed"} and attempt.verdict in {"PASS", "FAIL"}
        )]
        completed = {attempt.item_id for attempt in authentic}
        linked = {attempt.item_id for attempt in authentic
                  if (attempt.approved_draft_id, attempt.item_id) in executable_pairs}
        metrics.update({
            "generated_candidates": len(generated),
            "accepted_candidates": len(accepted),
            "conversion_success_candidates": len(converted),
            "draft_acceptance_rate": len(accepted) / len(generated) if generated else None,
            "executable_conversion_success_rate": len(converted) / len(generated) if generated else None,
            "real_authenticated_attempts": len(authentic),
            "real_completed_viewpoints": len(completed),
            "synthetic_attempts": sum(attempt.evidence_kind == "synthetic" for attempt in snapshot.runs),
        })
        selected = {item.item_id for item in pilot.viewpoints}
        if approved != selected:
            missing.append("approved_procedure_coverage")
        if executable_accepted != selected:
            missing.append("live_converted_procedure_coverage")
        if completed != selected:
            missing.append("real_execution_coverage")
        if linked != selected:
            missing.append("approved_generation_execution_link")
        if not missing:
            baseline = pilot.timings.manual_baseline_minutes
            assisted = pilot.timings.assisted_total_minutes
            saved_minutes = baseline - assisted
            saved_percent = 100 * (saved_minutes / baseline)
            if not math.isfinite(saved_minutes) or not math.isfinite(saved_percent):
                saved_minutes = None
                saved_percent = None
                missing.append("timing_ratio_out_of_range")
    return {
        "schema_version": 1,
        "project_id": pilot.project_id,
        "selection_sha256": digest,
        "source": "postgresql_readonly" if verified_source else "unverified",
        "modules": list(pilot.modules),
        "selected_viewpoints": len(pilot.viewpoints),
        "readiness": readiness,
        "operator_timings": timings,
        "metrics": metrics,
        "measurement": {
            "complete": not missing,
            "missing": missing,
            "reported_time_saved_minutes": saved_minutes,
            "reported_time_saved_percent": saved_percent,
            "requires_owner_review": True,
            "rollout_approved": False,
        },
        "provenance": {
            "timings": "operator_recorded",
            "viewpoint_approval": "operator_declared",
            "document_revision": "operator_declared",
        },
    }
