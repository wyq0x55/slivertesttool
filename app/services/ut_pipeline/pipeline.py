"""Project binding, 実施一覧 import, batches and the per-target job state machine.

::

    queued → generating ─┬─ generator TestCsv ─────────────→ executing
                         └─ no TestCsv / bridge failure ───→ needs_human | error
    executing  (ut-agent typed candidate → LLVM compile/execute/coverage/check)
        PASS                               → awaiting_review
        PASS with open missing cases,
          LLM rounds left                  → repairing → executing → ...
        FAIL, LLM rounds left              → repairing → executing → ...
        FAIL that input rows cannot close  → needs_human
        FAIL, rounds exhausted             → needs_human
        FAIL on a human revision           → needs_human
        BLOCKED by missing execution tools   → blocked (retryable)
        BLOCKED otherwise                  → needs_human
    human gates (awaiting_review, needs_human, blocked, error):
        upload revision → executing · approve → approved · reject → rejected
        retry (blocked, error) → executing | repairing
    stop (any active state) → stopped, at the next step boundary
        stopped accepts the human-gate actions; retry resumes it

No machine outcome is final: every path ends at a human gate.  A job whose
target was queued again is superseded: it stays visible but read-only, and
only the target's latest job counts as work.

A missing case is an obligation the generator could not give a proven input
row for (``gap_class`` MISSING_CASE in the completeness report).  The model
closes it only by claiming it with an appended row or by declining it; a claim
is a candidate for the reviewer, never proof, because the completeness report
is the generator's own evidence and does not change with appended rows.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

from flask import current_app
from sqlalchemy import func

from ...extensions import db
from ...models import LMUser, UtAttempt, UtBatch, UtJob, UtPipelineConfig, UtTarget
from ...models.ut_pipeline import GateCode
from ...models.lanmatrix import _iso, _utcnow
from ..ai.provider import ProviderError
from ..lanmatrix import audit
from ..lanmatrix import notification_service as notify_svc
from ..lanmatrix.errors import ServiceError, VersionConflict
from ..lanmatrix.permissions import PermissionDenied
from . import (
    binding,
    diagnostic,
    handoff,
    host,
    host_candidate,
    repair,
    repair_source,
    server_paths,
    timing,
    toolchains,
    witness,
)
from .bridge import BridgeError, BridgeSettings, UtAgentBridge

# Shared extraction is serialised; LLVM executions and model calls can overlap.
_GENERATION_GATE = threading.Lock()
_EXPECTED_ATTEMPT_UNSET = object()

_CONFIG_TEXT_FIELDS = ("manifest_path", "index_csv", "product_root",
                       "toolchain_profile",
                       "repair_model", "stub_mode",
                       "stub_model", "host_clang", "host_llvm_cov", "host_llvm_profdata",
                       "host_profile_runtime")
# Numeric settings that may stay undeclared; NULL keeps the ut-agent default.
_OPTIONAL_NUMERIC_FIELDS = (("function_step_limit", 1, 1_000_000_000),)
_REQUIRED_FIELDS = ("manifest_path", "index_csv", "product_root")
_JOB_LIST_LIMIT = 500


def get_bridge() -> UtAgentBridge:
    return UtAgentBridge(BridgeSettings.from_config(current_app.config))


# --------------------------------------------------------------------------- #
# Project binding
# --------------------------------------------------------------------------- #
def get_config(project_id: int) -> Optional[UtPipelineConfig]:
    return UtPipelineConfig.query.filter_by(project_id=project_id).first()


def config_document(config: Optional[UtPipelineConfig]) -> Optional[dict]:
    if config is None:
        return None
    result = dict(config.to_dict(), manifest_document=None, manifest_sha256=None, manifest_error=None)
    if config.manifest_path:
        try:
            result.update(binding.read_manifest(config.manifest_path, bridge=get_bridge()))
        except (OSError, ValueError, TypeError, BridgeError):
            result["manifest_error"] = "现有项目清单无法读取或未通过校验，请重新导入或填写清单。"
    return result


def validate_manifest(project_id: int, document: object) -> dict:
    return binding.prepare_manifest(project_id, document, bridge=get_bridge(), workspace=_workspace(), persist=False)


def manifest_options() -> dict:
    """Offer installed, authoritative choices without selecting project semantics."""
    try:
        result = get_bridge().configuration_options()
    except (BridgeError, KeyError, TypeError) as exc:
        raise ServiceError("无法加载项目清单可选配置，请检查 ut-agent 版本与连接后刷新页面。",
                           code="UT_CONFIGURATION_OPTIONS_UNAVAILABLE") from exc
    if (not isinstance(result, dict)
            or result.get("contract") != "ut-agent.configuration-options/2"
            or not all(isinstance(result.get(key), list) and result.get(key)
                       for key in ("pointer_sizes", "build_profiles"))):
        raise ServiceError("ut-agent 返回了无效的项目清单选项，请升级配套工具后刷新页面。",
                           code="UT_CONFIGURATION_OPTIONS_UNAVAILABLE")
    return result


def require_config(project_id: int) -> UtPipelineConfig:
    config = get_config(project_id)
    if config is None or not all(getattr(config, key) for key in _REQUIRED_FIELDS):
        raise ServiceError(
            "请先配置 ut-agent 项目绑定（ProjectManifest / 索引 CSV / Product 根）",
            code="UT_NOT_CONFIGURED")
    if config.execution_backend != "clang":
        raise ServiceError("单元测试作业仅支持 Clang/LLVM，请保存 LLVM 项目配置", code="UT_NOT_CONFIGURED")
    return config


def frozen_step_limit(config: dict[str, Any]) -> Optional[int]:
    """The batch's frozen per-function ceiling; ``None`` leaves ut-agent's own limit."""
    value = config.get("function_step_limit")
    if value is None:
        return None
    if type(value) is not int or value < 1:
        raise BridgeError("Invalid frozen function_step_limit")
    return value


def _step_limit_unsupported(exc: Exception, limit: Optional[int]) -> bool:
    """True when this ut-agent predates ``--function-step-limit``.

    The flag is rejected by argparse before any work starts, so the batch fails
    with one subprocess call and no partial generation. PR #255 did not add a
    capability key to ``configuration-options``, so a version probe cannot
    distinguish the builds; the launch itself is the only reliable test.
    """
    return bool(limit) and "unrecognized arguments: --function-step-limit" in str(exc)


def save_config(project_id: int, values: dict[str, Any], user_id: int) -> UtPipelineConfig:
    existing = get_config(project_id)
    before = existing.to_dict() if existing else None
    actor = db.session.get(LMUser, user_id) if user_id else None
    is_admin = bool(actor and actor.is_system_admin)
    normalized = {}
    for key in _CONFIG_TEXT_FIELDS:
        if key in values:
            if not isinstance(values[key], str):
                raise ServiceError(f"{key} must be a string", code="VALIDATION_ERROR")
            if any(ch in values[key] for ch in ("\x00", "\r", "\n")):
                raise ServiceError(f"{key} contains invalid control characters", code="VALIDATION_ERROR")
            value = values[key].strip()
            limit = 64 if key == "toolchain_profile" else (128 if key in ("stub_model", "repair_model") else 1024)
            if len(value) > limit:
                raise ServiceError(f"{key} exceeds {limit} characters", code="VALIDATION_ERROR")
            normalized[key] = value
    stub_mode = normalized.get("stub_mode", getattr(existing, "stub_mode", "deterministic"))
    if stub_mode not in ("deterministic", "llm"):
        raise ServiceError("stub_mode must be one of deterministic, llm", code="VALIDATION_ERROR")
    normalized["stub_mode"] = stub_mode
    # Recorded fact, not a choice: every job executes on Clang/LLVM.
    normalized["execution_backend"] = "clang"
    profile_explicit = "toolchain_profile" in normalized
    eff_profile_id = (
        normalized["toolchain_profile"]
        if profile_explicit
        else (getattr(existing, "toolchain_profile", None) or "default")
    ) or "default"
    selected_profile = toolchains.resolve_profile(eff_profile_id)
    normalized["toolchain_profile"] = selected_profile["id"]
    raw_host_override = any(key in normalized for key in toolchains.TOOLCHAIN_KEYS)
    for key in toolchains.TOOLCHAIN_KEYS:
        if key in normalized:
            continue
        if profile_explicit or existing is None or getattr(existing, key, None) is None:
            normalized[key] = selected_profile[key]
        else:
            normalized[key] = getattr(existing, key)
    toolchains.verify_toolchain_config(
        normalized,
        allow_custom_admin=(
            is_admin
            or (existing is not None and not raw_host_override and not profile_explicit)
        ),
        admin_override_authorized=bool(is_admin and raw_host_override),
        project_id=project_id,
    )
    for key, low, high in (("max_repair_rounds", 0, 20), ("stub_max_rounds", 1, 5)):
        if key in values:
            value = values[key]
            if type(value) is not int or not low <= value <= high:
                raise ServiceError(f"{key} must be an integer from {low} to {high}", code="VALIDATION_ERROR")
            normalized[key] = value
    for key, low, high in _OPTIONAL_NUMERIC_FIELDS:
        if key not in values:
            continue
        value = values[key]
        if value is None or value == "":
            normalized[key] = None
            continue
        if type(value) is not int or not low <= value <= high:
            raise ServiceError(f"{key} must be an integer from {low} to {high}, or empty",
                               code="VALIDATION_ERROR")
        normalized[key] = value
    explicit_roots = server_paths.parse_roots(current_app.config.get("UT_ALLOWED_SOURCE_ROOTS"))
    if not is_admin or explicit_roots:
        source_roots = (explicit_roots
                        if explicit_roots
                        else server_paths.configured_roots(current_app.config, project_id=project_id))
        for key in ("product_root", "index_csv"):
            val = normalized.get(key)
            if val and not (existing is not None and val == getattr(existing, key, None)):
                server_paths.assert_allowed_path(val, source_roots, exists=False)
        raw_manifest = normalized.get("manifest_path")
        if ("manifest_document" not in values and raw_manifest
                and not (existing is not None and raw_manifest == getattr(existing, "manifest_path", None))):
            manifest_roots = [*source_roots, _workspace() / f"p{project_id}" / "manifests"]
            server_paths.assert_allowed_path(raw_manifest, manifest_roots, exists=False)
    if "manifest_document" in values:
        if normalized.get("manifest_path"):
            raise ServiceError("请提交清单内容，不要同时提交服务器清单路径", code="VALIDATION_ERROR")
        prepared = binding.prepare_manifest(project_id, values["manifest_document"], bridge=get_bridge(),
                                            workspace=_workspace(), persist=True)
        normalized["manifest_path"] = prepared["path"]
    config = existing or UtPipelineConfig(
        project_id=project_id,
        max_repair_rounds=current_app.config["UT_REPAIR_MAX_ROUNDS"],
    )
    for key, value in normalized.items():
        setattr(config, key, value)
    config.updated_by = user_id
    db.session.add(config)
    db.session.flush()
    audit.record("ut.config.update", actor_id=user_id, object_type="ut_config",
                 object_id=project_id, project_id=project_id,
                 old_value=before, new_value=config.to_dict())
    db.session.commit()
    return config


# --------------------------------------------------------------------------- #
# 実施一覧
# --------------------------------------------------------------------------- #
def import_targets(project_id: int, user_id: Optional[int] = None) -> dict[str, int]:
    """Mirror the project index; rows that disappeared are kept but flagged."""
    config = require_config(project_id)
    try:
        rows = get_bridge().index(config.manifest_path, config.index_csv, config.product_root)
    except BridgeError as exc:
        raise ServiceError(f"索引读取失败：{exc}", code="UT_BRIDGE_ERROR") from exc
    existing = {(target.source_rel, target.function): target
                for target in UtTarget.query.filter_by(project_id=project_id)}
    seen = set()
    added = 0
    now = _utcnow()
    for row in rows:
        key = (row["source_rel"], row["function"])
        seen.add(key)
        target = existing.get(key)
        if target is None:
            target = UtTarget(project_id=project_id, source_rel=key[0], function=key[1])
            db.session.add(target)
            added += 1
        target.row_no = row["row"]
        target.callcnt = row["callcnt"]
        target.source_name = row["source_name"]
        target.unit_rel = row["unit_rel"]
        target.in_index = True
        target.imported_at = now
    removed = 0
    for key, target in existing.items():
        if key not in seen and target.in_index:
            target.in_index = False
            removed += 1
    summary = {"total": len(rows), "added": added, "removed": removed}
    audit.record("ut.targets.import", actor_id=user_id, object_type="ut_index",
                 object_id=project_id, project_id=project_id, new_value=summary)
    db.session.commit()
    return summary


def _job_summary(job: Optional[UtJob]) -> Optional[dict[str, Any]]:
    if job is None:
        return None
    return {"id": job.id, "batch_id": job.batch_id, "state": job.state,
            "repair_rounds": job.repair_rounds, "last_verdict": job.last_verdict or None,
                "execution_backend": "clang"}


def list_targets(project_id: int) -> list[dict[str, Any]]:
    targets = (UtTarget.query.filter_by(project_id=project_id)
               .order_by(UtTarget.in_index.desc(), UtTarget.row_no).all())
    job_ids = [target.latest_job_id for target in targets if target.latest_job_id]
    jobs = ({job.id: job for job in UtJob.query.filter(UtJob.id.in_(job_ids))}
            if job_ids else {})
    result = []
    for target in targets:
        entry = target.to_dict()
        entry["latest_job"] = _job_summary(jobs.get(target.latest_job_id))
        result.append(entry)
    return result


# --------------------------------------------------------------------------- #
# Batches
# --------------------------------------------------------------------------- #
def create_batch(project_id: int, target_ids: Any, user_id: int, label: str = "",
                 *, derived_from: Optional[int] = None) -> UtBatch:
    config = require_config(project_id)
    if (not isinstance(target_ids, list) or not target_ids
            or any(type(item) is not int for item in target_ids)):
        raise ServiceError("target_ids 必须是非空整数数组", code="VALIDATION_ERROR")
    ids = list(dict.fromkeys(target_ids))
    targets = UtTarget.query.filter(UtTarget.project_id == project_id,
                                    UtTarget.id.in_(ids)).all()
    if len(targets) != len(ids):
        raise ServiceError("包含不属于该项目的目标", code="NOT_FOUND")
    stale = sorted(target.function for target in targets if not target.in_index)
    if stale:
        raise ServiceError(f"目标已不在当前索引中：{', '.join(stale)}", code="VALIDATION_ERROR")
    busy = UtJob.query.filter(UtJob.target_id.in_(ids), UtJob.state.in_(UtJob.ACTIVE)).all()
    if busy:
        raise ServiceError(
            "以下函数已在队列中：" + ", ".join(sorted({job.target.function for job in busy})),
            code="DUPLICATE")
    batch = UtBatch(project_id=project_id, label=(label or "")[:128], created_by=user_id,
                    status=UtBatch.STATUS_GENERATING, config_snapshot=config.to_dict(),
                    derived_from_batch_id=derived_from)
    db.session.add(batch)
    db.session.flush()
    for target in sorted(targets, key=lambda item: item.row_no):
        job = UtJob(batch_id=batch.id, project_id=project_id, target_id=target.id,
                    max_repair_rounds=config.max_repair_rounds)
        db.session.add(job)
        db.session.flush()
        timing.initialize(job)
        previous = (UtJob.query.filter_by(id=target.latest_job_id)
                    .with_for_update(of=UtJob).populate_existing().one()) if target.latest_job_id else None
        if previous is not None:
            timing.supersede(previous)
        target.latest_job_id = job.id
    audit.record("ut.batch.create", actor_id=user_id, object_type="ut_batch",
                 object_id=batch.id, project_id=project_id,
                 new_value={"functions": [t.function for t in targets], "label": batch.label})
    db.session.commit()
    return batch


def derive_batch(project_id: int, batch_id: int, user_id: int, body: dict[str, Any]) -> UtBatch:
    """Queue a new batch for the functions a confirmed witness run changed.

    The source batch is read and never written: its frozen config, provenance and
    status stay exactly as they were delivered. The new batch freezes the config
    current at the moment of derivation, so a different step ceiling applies only
    to it.
    """
    source = UtBatch.query.filter_by(id=batch_id, project_id=project_id).one_or_none()
    if source is None:
        raise ServiceError("批次不存在", code="NOT_FOUND")
    requested = body.get("targets")
    if requested is None:
        requested = _confirmed_targets(source)
    elif not isinstance(requested, list) or any(type(item) is not int for item in requested):
        raise ServiceError("targets 必须是目标 id 数组", code="VALIDATION_ERROR")
    if not requested:
        raise ServiceError("没有可重生成的目标：需要已确认的接手记录", code="VALIDATION_ERROR")
    limit = body.get("function_step_limit", _UNCHANGED)
    if limit is not _UNCHANGED:
        _apply_step_limit(project_id, limit, user_id)
    label = body.get("label")
    if label is not None and not isinstance(label, str):
        raise ServiceError("label 必须是字符串", code="VALIDATION_ERROR")
    return create_batch(project_id, requested, user_id, label or "", derived_from=source.id)


_UNCHANGED = object()


def _confirmed_targets(batch: UtBatch) -> list[int]:
    """Targets whose witness run confirmed entries, one per function."""
    jobs = UtJob.query.filter_by(batch_id=batch.id).all()
    chosen: dict[str, int] = {}
    for job in jobs:
        witness = job.witness if isinstance(job.witness, dict) else {}
        record = witness.get("handoff") if isinstance(witness.get("handoff"), dict) else {}
        confirm = witness.get("confirm") if isinstance(witness.get("confirm"), dict) else {}
        if record.get("state") not in ("HANDOFF_READY", "NO_ADMISSIBLE_OBLIGATION"):
            continue
        if confirm.get("status") != "CONFIRMED":
            continue
        chosen.setdefault(job.target.function, job.target_id)
    return list(chosen.values())


def _apply_step_limit(project_id: int, value: Any, user_id: int) -> None:
    """Save a step ceiling for batches created after this one.

    Only the project config changes, so every batch already frozen keeps the
    ceiling it was created with.
    """
    save_config(project_id, {"function_step_limit": value}, user_id)


def list_batches(project_id: int) -> list[dict[str, Any]]:
    batches = (UtBatch.query.filter_by(project_id=project_id)
               .order_by(UtBatch.id.desc()).limit(100).all())
    counts: dict[int, dict[str, int]] = {}
    if batches:
        rows = (db.session.query(UtJob.batch_id, UtJob.state, func.count(UtJob.id))
                .filter(UtJob.batch_id.in_([batch.id for batch in batches]))
                .group_by(UtJob.batch_id, UtJob.state).all())
        for batch_id, state, count in rows:
            counts.setdefault(batch_id, {})[state] = count
    return [dict(batch.to_dict(), states=counts.get(batch.id, {})) for batch in batches]


def _workspace() -> Path:
    return Path(current_app.config["WORKSPACE_DIR"]) / "ut"


def _batch_dir(project_id: int, batch_id: int) -> Path:
    return _workspace() / f"p{project_id}" / f"b{batch_id}"


def _generation_progress_dir(project_id: int, batch_id: int) -> Path:
    return _batch_dir(project_id, batch_id) / "generation-progress"


# ut-agent's read-only budget forecast of the functions a batch is generating.
_FORECAST_SCHEMA = "ut-agent.progress-forecast/1"
_BUDGET_EXHAUSTED = "GENERATION_BUDGET_EXHAUSTED"


def _forecast_history_dir(project_id: int, batch_id: int) -> Path:
    return _batch_dir(project_id, batch_id) / "forecast-history"


def _forecast_history(job: UtJob) -> dict[str, Any]:
    """What the same target did last time — the only history the forecast uses.

    Only a run that already exhausted its semantic step budget carries signal;
    any other earlier outcome says nothing about this one's budget fit.
    """
    previous = (UtJob.query
                .filter(UtJob.project_id == job.project_id,
                        UtJob.target_id == job.target_id,
                        UtJob.id < job.id)
                .order_by(UtJob.id.desc())
                .first())
    if previous is None or _BUDGET_EXHAUSTED not in (previous.reason or ""):
        return {}
    return {"budget_exhausted": True, "source_job": previous.id}


def _write_forecast_history(directory: Path, jobs: list[UtJob]) -> None:
    for job in jobs:
        function = job.target.function if job.target else ""
        if not _C_IDENTIFIER.fullmatch(function):
            continue
        _write_json_atomic(directory / f"{function}.json", _forecast_history(job))


def _write_json_atomic(path: Path, document: dict[str, Any]) -> None:
    """Observation file.  A disk problem here must never disturb generation."""
    temporary = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    except (OSError, TypeError, ValueError):
        pass


def _forecast_record(report: dict[str, Any], observed_at: str) -> dict[str, Any]:
    """The verdict as the platform keeps it: no server paths, no snapshot text."""
    functions: dict[str, Any] = {}
    for name, verdict in (report.get("functions") or {}).items():
        if isinstance(verdict, dict):
            functions[str(name)] = {key: value for key, value in verdict.items()
                                    if key != "snapshot"}
    return {
        "schema": report.get("schema") or _FORECAST_SCHEMA,
        "observed_at": observed_at,
        "intervene": bool(report.get("intervene")),
        "intervening": [str(name) for name in report.get("intervening") or ()],
        "functions": functions,
    }


def _job_dir(job: UtJob) -> Path:
    return _batch_dir(job.project_id, job.batch_id) / f"j{job.id}"


def _gate(job: UtJob, state: str, reason: str = "", code: str = "") -> None:
    reached_gate = state in UtJob.HUMAN_GATES and job.state != state
    _set_state(job, state)
    job.reason = reason
    job.gate_code = code if state in UtJob.ACTIONABLE else ""
    if reached_gate:
        _notify_requester(job, notify_svc.TASK_FINISHED, _GATE_TITLES[state], reason)


def _set_state(job: UtJob, state: str) -> None:
    if job.state == UtJob.GENERATING and state != UtJob.GENERATING:
        timing.record_generation(job, _generation_progress(job))
    timing.transition(job, state)
    job.state = state


_GATE_TITLES = {
    UtJob.AWAITING_REVIEW: "单元测试待审核", UtJob.NEEDS_HUMAN: "单元测试需人工处理",
    UtJob.BLOCKED: "单元测试环境阻塞", UtJob.ERROR: "单元测试异常",
}


def _notify_requester(job: UtJob, kind: str, title: str, body: str = "",
                      actor_id: Optional[int] = None) -> None:
    """Tell whoever queued the batch; joins the caller's transaction."""
    from ...models import LMUser
    from ..lanmatrix import permissions, users_service

    batch = db.session.get(UtBatch, job.batch_id)
    recipient = db.session.get(LMUser, batch.created_by) if batch and batch.created_by else None
    # Membership can be revoked while a batch runs; the notification carries
    # job details, so it follows the same visibility rule as the project.
    if recipient is None or not recipient.is_active or not permissions.can(
            "project.view", users_service.role_in_project(job.project_id, recipient),
            is_system_admin=recipient.is_system_admin):
        return
    target = job.target
    name = target.function if target else f"作业 #{job.id}"
    notify_svc.notify(
        recipient.id, kind, f"{title}：{name}", body=body,
        project_id=job.project_id,
        link_url=f"/lanmatrix/projects/{job.project_id}/tasks?job={job.id}",
        ref_type="ut_job", ref_id=str(job.id), actor_id=actor_id)


def _new_attempt(job: UtJob, origin: str, csv_bytes: Optional[bytes], *,
                 note: str = "", reviser: Optional[dict] = None,
                 user_id: Optional[int] = None, make_current: bool = True) -> UtAttempt:
    seq = (db.session.query(func.max(UtAttempt.seq)).filter_by(job_id=job.id).scalar() or 0) + 1
    path = digest = ""
    if csv_bytes is not None:
        directory = _job_dir(job)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"a{seq:02d}-{origin.lower()}.csv"
        target.write_bytes(csv_bytes)
        path, digest = str(target), hashlib.sha256(csv_bytes).hexdigest()
    metadata = {**(reviser or {}), "parent_attempt_id": job.current_attempt_id}
    attempt = UtAttempt(job_id=job.id, seq=seq, origin=origin, csv_path=path,
                        csv_sha256=digest, reviser=metadata, note=note, created_by=user_id)
    db.session.add(attempt)
    db.session.flush()
    if path and make_current:
        job.current_attempt_id = attempt.id
    return attempt


def _refresh_batch(batch_id: int) -> None:
    batch = db.session.get(UtBatch, batch_id)
    if batch is None or batch.status != UtBatch.STATUS_RUNNING:
        return
    active = UtJob.query.filter(UtJob.batch_id == batch_id,
                                UtJob.state.in_(UtJob.ACTIVE)).count()
    if not active:
        batch.status = UtBatch.STATUS_DONE
        batch.finished_at = _utcnow()
        db.session.commit()


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #
class GenerationBusy(RuntimeError):
    """Another batch owns shared extraction; reschedule without tying up a worker."""


def _freeze_witness(batch_id: int, generation_dir: Path) -> None:
    """Store, per job, what a witness run could take from this generation.

    Observation only: the record never changes a job's state and never becomes
    evidence.  It is recomputed on every generation attempt (including a retry)
    so the stored answer always belongs to the newest report.
    """
    jobs = (UtJob.query.filter_by(batch_id=batch_id)
            .populate_existing().all())
    for job in jobs:
        job.witness = handoff.freeze(job, generation_dir)


def run_generation(batch_id: int, *, on_ready: Callable[[int], None] | None = None) -> list[int]:
    """Dispatch finalized functions while the single shared generation continues."""
    if not _GENERATION_GATE.acquire(blocking=False):
        raise GenerationBusy("Another batch is generating")
    try:
        return _run_generation(batch_id, on_ready)
    finally:
        _GENERATION_GATE.release()


def _run_generation(batch_id: int, on_ready: Callable[[int], None] | None) -> list[int]:
    batch = db.session.get(UtBatch, batch_id)
    if batch is None or batch.status != UtBatch.STATUS_GENERATING:
        return []
    jobs = (UtJob.query.filter_by(batch_id=batch_id, state=UtJob.QUEUED)
            .order_by(UtJob.id).all())
    if not jobs:
        interrupted = (UtJob.query.filter_by(batch_id=batch_id, state=UtJob.GENERATING)
                       .with_for_update(of=UtJob).populate_existing().all())
        if interrupted:
            # A delayed duplicate cannot resume a generator that died after
            # claiming its jobs, nor certify the unfinished batch as done.
            for job in interrupted:
                timing.interrupt(job)
                if job.stop_requested_at is not None:
                    _halt(job)
                else:
                    _gate(job, UtJob.ERROR, "生成进程中断；请在「任务/运行」重新入队", GateCode.INTERRUPTED)
            batch.status = UtBatch.STATUS_FAILED
            batch.error = "生成进程中断，重复任务不能接管未完成的生成"
            batch.finished_at = _utcnow()
        else:
            batch.status = UtBatch.STATUS_RUNNING
        db.session.commit()
        _refresh_batch(batch_id)
        return []
    for job in jobs:
        _set_state(job, UtJob.GENERATING)
    db.session.commit()
    config = batch.config_snapshot or {}
    bridge = get_bridge()
    by_function = {job.target.function: job.id for job in jobs}
    # The bridge selects by function name from the live index, so the source the
    # unit was generated from must still be the source this target was queued for.
    sources = {job.target.function: job.target.source_rel for job in jobs}
    handed: dict[str, dict] = {}
    ready = []

    def accept(unit: dict, provenance: dict | None, expected_sha: str | None = None) -> None:
        name = unit.get("function")
        if name not in by_function:
            raise BridgeError("Completed unit is outside the requested batch")
        if unit.get("source_rel") != sources[name]:
            raise BridgeError(
                f"生成单元的源文件 {unit.get('source_rel')!r} 与排队目标 {sources[name]!r} 不一致，"
                "索引已变化，请重新导入索引后再生成")
        active_batch = db.session.get(UtBatch, batch_id, populate_existing=True)
        if active_batch.provenance is not None and active_batch.provenance != provenance:
            raise BridgeError("Completed units disagree on generation provenance")
        if name in handed:
            if handed[name] != unit:
                raise BridgeError("Final generation document conflicts with a completed unit")
            db.session.commit()
            return
        active_batch.provenance = provenance
        job = (UtJob.query.filter_by(id=by_function[name], batch_id=batch_id)
               .with_for_update(of=UtJob).populate_existing().one())
        if job.state != UtJob.GENERATING:
            handed[name] = dict(unit)
            db.session.commit()
            return
        job.generation_status = str(unit.get("status") or "")
        recorded = (job.generation or {}).get(timing.KEY)
        job.generation = {**unit, "execution_backend": "clang",
                          **({timing.KEY: recorded} if recorded is not None else {})}
        if not unit.get("testcsv"):
            if job.stop_requested_at is not None:
                _halt(job)
            else:
                _gate(job, UtJob.NEEDS_HUMAN,
                      diagnostic.generation_failure_reason(unit), GateCode.NO_TESTCSV)
        else:
            try:
                data = Path(unit["testcsv"]).read_bytes()
            except OSError as exc:
                raise BridgeError("Completed TestCsv could not be read: " + str(exc)) from exc
            if expected_sha is not None and hashlib.sha256(data).hexdigest() != expected_sha:
                raise BridgeError("Completed TestCsv changed after receipt verification")
            attempt = _new_attempt(job, UtAttempt.ORIGIN_GENERATOR, data,
                                   note=f"generation status {job.generation_status}")
            job.reference_csv = attempt.csv_path
            if job.stop_requested_at is not None:
                _halt(job)
            else:
                _set_state(job, UtJob.EXECUTING)
        handed[name] = dict(unit)
        execute = job.state == UtJob.EXECUTING
        db.session.commit()
        if execute:
            ready.append(job.id)
            if on_ready is not None:
                try:
                    on_ready(job.id)
                except Exception as exc:
                    db.session.rollback()
                    job = db.session.get(UtJob, by_function[name], populate_existing=True)
                    if job.state == UtJob.EXECUTING:
                        _gate(job, UtJob.ERROR, "完成生成后投递执行失败：" + str(exc), GateCode.INTERNAL_ERROR)
                        db.session.commit()
                    raise BridgeError("Could not dispatch a completed function") from exc

    def completed(receipt: dict) -> None:
        if receipt.get("contract") != "ut-agent.generated-unit/1":
            raise BridgeError("Unknown completed-unit receipt contract")
        unit = receipt["unit"]
        digest = ((receipt.get("artifacts") or {}).get("testcsv") or {}).get("sha256")
        if unit.get("testcsv") and not digest:
            raise BridgeError("Completed TestCsv receipt has no digest")
        accept(unit, receipt["provenance"], digest)

    # Declared before the attempt: a constraint failure precedes the frozen read.
    limit: Optional[int] = None
    generation_dir = _batch_dir(batch.project_id, batch.id) / "generation"
    try:
        batch.constraints = bridge.constraints(config["manifest_path"])
        db.session.commit()
        limit = frozen_step_limit(config)
        progress_dir = _generation_progress_dir(batch.project_id, batch.id)
        # History first: the forecast reads one document per function, so the
        # in-flight run cannot observe itself.
        history_dir = _forecast_history_dir(batch.project_id, batch.id)
        _write_forecast_history(history_dir, jobs)

        def observe_forecast(report: dict[str, Any]) -> None:
            _write_json_atomic(progress_dir / "forecast.json",
                               _forecast_record(report, _iso(_utcnow())))

        document = bridge.generate(
            config["manifest_path"], config["index_csv"], config["product_root"],
            generation_dir,
            list(by_function),
            progress_dir=progress_dir,
            on_unit=completed,
            on_forecast=observe_forecast,
            history_dir=history_dir,
            function_step_limit=limit,
        )
        for unit in document.get("units", ()):
            accept(unit, document.get("provenance"))
        if set(by_function) - handed.keys():
            raise BridgeError("生成结果中缺少该函数")
    except BridgeError as exc:
        db.session.rollback()
        batch = db.session.get(UtBatch, batch_id, populate_existing=True)
        batch.status = UtBatch.STATUS_FAILED
        batch.error = str(exc)
        batch.finished_at = _utcnow()
        unfinished = (UtJob.query.filter_by(batch_id=batch_id, state=UtJob.GENERATING)
                      .with_for_update(of=UtJob).populate_existing().all())
        unsupported = _step_limit_unsupported(exc, limit)
        for job in unfinished:
            if job.stop_requested_at is not None:
                _halt(job)
            elif unsupported:
                _gate(job, UtJob.BLOCKED,
                      "当前 ut-agent 不支持 --function-step-limit：请升级到包含该参数的版本，"
                      "或把「函数语义步数上限」留空后重建批次。",
                      GateCode.ENV_BLOCKED)
            else:
                _gate(job, UtJob.ERROR, f"生成失败：{exc}", GateCode.GENERATION_FAILED)
        _freeze_witness(batch_id, generation_dir)
        db.session.commit()
        return ready
    _freeze_witness(batch_id, generation_dir)
    batch = db.session.get(UtBatch, batch_id, populate_existing=True)
    batch.status = UtBatch.STATUS_RUNNING
    db.session.commit()
    _refresh_batch(batch.id)
    return ready


# --------------------------------------------------------------------------- #
# Execution / check / repair loop
# --------------------------------------------------------------------------- #
def _error_codes(check: dict[str, Any]) -> list[str]:
    return [issue["code"] for issue in check.get("issues", ()) if issue.get("severity") == "error"]




_MISSING_CASE = "MISSING_CASE"


def _verified_obligation_ids(check: dict[str, Any], expected_sha: str = "",
                             execution: dict[str, Any] | None = None) -> set[str]:
    witnesses = check.get("candidate_witnesses") or {}
    identified = (witnesses.get("contract") == "ut-agent.candidate-witness/1"
                  and (witnesses.get("evidence") or {}).get("identified") is True
                  and witnesses.get("candidate_sha256") == check.get("candidate_sha256")
                  and bool(check.get("candidate_sha256"))
                  and (not expected_sha or expected_sha == check["candidate_sha256"]))
    return (set(witnesses.get("witnessed_obligation_ids") or ())
            if identified and not diagnostic.integrity_errors(check, expected_sha, execution) else set())


def gap_status(check: dict[str, Any], revisers: list[dict[str, Any]],
               expected_sha: str = "", execution: dict[str, Any] | None = None) -> dict[str, Any]:
    """Missing cases of ``check`` against the claims and declines of model rounds.

    Only an explicit MISSING_CASE is work for the model; evidence gaps,
    unconfirmed infeasibility and reports without gap classes are not.
    """
    unresolved = diagnostic.report_records(check, "unresolved")
    report_complete = diagnostic.gap_report(check)["details_complete"]
    missing = [item for item in unresolved
               if item.get("gap_class") == _MISSING_CASE and isinstance(item.get("obligation_id"), str)
               and item["obligation_id"].strip()
               and (not isinstance(item.get("requirement"), dict)
                    or item["requirement"].get("configuration_status") != "NEEDS_REVIEW")]
    claimed: set[str] = set()
    declined: set[str] = set()
    for meta in revisers:
        claimed.update((meta or {}).get("claimed") or ())
        declined.update((meta or {}).get("declined") or ())
    ids = {item["obligation_id"] for item in missing}
    proven = _verified_obligation_ids(check, expected_sha, execution)
    return {
        "report_complete": report_complete,
        "configuration_conflicts": diagnostic.configuration_conflicts(check),
        "missing": len(ids),
        "claimed": len(ids & claimed),
        "witnessed": len(ids & proven),
        "unproven": len(ids - proven),
        "declined": len((ids & declined) - proven),
        "open": [item for item in missing
                 if item["obligation_id"] not in proven and item["obligation_id"] not in declined],
    }


def _gap_note(gaps: Optional[dict[str, Any]]) -> str:
    if not gaps or not gaps["missing"]:
        return ""
    return (f"；缺用例 {gaps['missing']} 个：候选输入已证实 {gaps.get('witnessed', 0)}，"
            f"模型补行认领 {gaps['claimed']}，"
            f"模型声明无法补齐 {gaps['declined']}，未处理 {len(gaps['open'])}")




def _report_gate(check: dict[str, Any], expected_sha: str = "",
                 execution: dict[str, Any] | None = None) -> tuple[str, str, str] | None:
    errors = diagnostic.integrity_errors(check, expected_sha, execution)
    if errors:
        return UtJob.NEEDS_HUMAN, "候选证据完整性异常：" + "; ".join(errors), GateCode.INTEGRITY_ERROR
    report = diagnostic.gap_report(check)
    if not report["details_complete"]:
        return (UtJob.NEEDS_HUMAN,
                "测试义务报告结构或明细不完整：" + "; ".join(report["errors"]) +
                "；请重新生成或重新校验以取得完整缺口清单，再核对输入证明。不能将预览视为全部完成。",
                GateCode.REPORT_INCOMPLETE)
    conflicts = diagnostic.configuration_conflicts(check)
    if conflicts:
        return (UtJob.NEEDS_HUMAN, "义务与冻结测试配置冲突，需核对版本并重新生成或校验：" + ", ".join(conflicts),
                GateCode.CONFIGURATION_CONFLICT)
    return None


def _integrity_check(check: dict[str, Any], reason: str) -> dict[str, Any]:
    """Retain the received report while refusing to grant it proof authority."""
    result = {**check, "verdict": "BLOCKED", "repairable": False, "delivery_validated": False,
              "evidence_integrity": {"status": "UNKNOWN", "reason": reason},
              "issues": [*(check.get("issues") or []),
                         {"code": "INTEGRITY_ERROR", "severity": "error", "repairable": False,
                          "message": reason}]}
    if check.get("candidate_witnesses"):
        witnesses = check["candidate_witnesses"]
        result["candidate_witnesses"] = {**witnesses, "input_mapping_identified": False,
            "evidence": {**(witnesses.get("evidence") or {}), "identified": False, "reason": reason}}
    return result


def _integrity_gate(job: UtJob, attempt: UtAttempt, reason: str,
                    check: dict[str, Any] | None = None,
                    execution: dict[str, Any] | None = None) -> None:
    attempt.check = _integrity_check((attempt.check or {}) if check is None else check, reason)
    if execution is not None:
        attempt.execution = execution
    attempt.verdict = job.last_verdict = "BLOCKED"
    _gate(job, UtJob.NEEDS_HUMAN, reason, GateCode.INTEGRITY_ERROR)


def _result_integrity(job: UtJob, attempt: UtAttempt, check: dict[str, Any],
                      execution: dict[str, Any] | None) -> bool:
    try:
        _read_attempt_csv(attempt)
        errors = diagnostic.integrity_errors(check, attempt.csv_sha256, execution)
        if errors:
            raise ServiceError("; ".join(errors), code="INTEGRITY_ERROR")
    except ServiceError as exc:
        _integrity_gate(job, attempt, "候选证据完整性异常：" + str(exc), check, execution)
        return False
    if not check.get("candidate_sha256"):
        check["candidate_identity"] = {
            "status": "UNKNOWN", "verified_attempt_sha256": attempt.csv_sha256,
            "reason": "Checker omitted its candidate digest; available execution digests were compared, not inferred",
        }
    return True




def _fresh_exec_dir(job: UtJob, attempt: UtAttempt) -> Path:
    base = _job_dir(job) / f"a{attempt.seq:02d}-exec"
    candidate, run = base, 1
    while candidate.exists():
        run += 1
        candidate = base.with_name(f"{base.name}{run}")
    return candidate


def _fresh_model_dir(job: UtJob, attempt: UtAttempt) -> Path:
    base = _job_dir(job) / f"a{attempt.seq:02d}-input-model-r{job.repair_rounds + 1:02d}"
    run = 1
    while (base / f"request-{run:02d}").exists():
        run += 1
    return base / f"request-{run:02d}"


def _execute_step(job: UtJob) -> None:
    attempt = db.session.get(UtAttempt, job.current_attempt_id)
    config = db.session.get(UtBatch, job.batch_id).config_snapshot or {}
    _execute_host_step(job, attempt, config)


# Errors that appended input rows can address. Expected values belong to the
# typed oracle, and format or column errors cannot be fixed by adding rows.
_ROW_FIXABLE = frozenset({"EXECUTION_PATH_DIVERGED", "ROW_COUNT_BELOW_REFERENCE",
                          "REFERENCE_CASE_REMOVED", "FORMAT_NO_DATA_ROWS"})


def _row_tags(check: dict[str, Any]) -> set[str]:
    """Error codes a model row may name besides a missing case (e.g. coverage)."""
    return {issue["code"] for issue in check.get("issues", ())
            if issue.get("severity") == "error" and issue.get("repairable")
            and (issue["code"].startswith("COVERAGE_") or issue["code"] in _ROW_FIXABLE)}


def next_state(check: dict[str, Any], origin: str, rounds: int, max_rounds: int,
               gaps: Optional[dict[str, Any]] = None) -> tuple[str, str, str]:
    """Pure transition after one checked attempt: (state, reason, gate code)."""
    verdict = check.get("verdict")
    codes = ", ".join(_error_codes(check))
    evidence_gate = _report_gate(check)
    if evidence_gate and (evidence_gate[2] != GateCode.REPORT_INCOMPLETE or verdict in {"PASS", "FAIL"}):
        return evidence_gate
    if gaps and gaps.get("report_complete") is False and verdict in {"PASS", "FAIL"}:
        return (UtJob.NEEDS_HUMAN,
                "测试义务明细不完整；请重新生成或重新校验以取得完整缺口清单，再核对输入证明。不能将预览视为全部完成。",
                GateCode.REPORT_INCOMPLETE)
    if verdict == "PASS":
        if gaps and gaps["open"] and origin != UtAttempt.ORIGIN_HUMAN:
            if rounds >= max_rounds:
                return (UtJob.NEEDS_HUMAN, f"仍有 {len(gaps['open'])} 个输入义务未证明，已到修订上限 {max_rounds}"
                        + _gap_note(gaps), GateCode.OPEN_GAPS_EXHAUSTED)
            return UtJob.REPAIRING, "", ""
        if gaps and gaps.get("unproven", gaps["missing"] - gaps.get("witnessed", 0)):
            return (UtJob.NEEDS_HUMAN, "Required input obligations remain unproven" + _gap_note(gaps),
                    GateCode.CHECK_BLOCKED)
        return UtJob.AWAITING_REVIEW, "机器校验通过，等待人工审核" + _gap_note(gaps), GateCode.MACHINE_PASS
    if verdict == "BLOCKED":
        environment_codes = {"HOST_TOOL_UNAVAILABLE", "HOST_PROFILE_RUNTIME_UNAVAILABLE"}
        errors = set(_error_codes(check))
        if errors and errors <= environment_codes:
            messages = "; ".join(
                str(issue.get("message") or issue["code"]) for issue in check.get("issues", ())
                if issue.get("code") in environment_codes and issue.get("severity") == "error")
            return (UtJob.BLOCKED, f"Clang/LLVM 执行工具或覆盖率运行库不可用：{messages}",
                    GateCode.ENV_BLOCKED)
        blocked = next((issue for issue in check.get("issues", ())
                        if issue["code"] == "EXECUTION_BLOCKED"), None)
        if blocked is not None:
            reasons = blocked.get("detail") or []
            listed = "; ".join(
                f"{item.get('code')} {item.get('detail') or item.get('path', '')}"
                for item in reasons[:5])
            return UtJob.BLOCKED, f"Clang/LLVM 执行环境不完整：{listed}", GateCode.ENV_BLOCKED
        return UtJob.NEEDS_HUMAN, diagnostic.blocked_reason(check, codes), GateCode.CHECK_BLOCKED
    if verdict != "FAIL":
        return UtJob.NEEDS_HUMAN, f"未知判定：{verdict}", GateCode.UNKNOWN_VERDICT
    if origin == UtAttempt.ORIGIN_HUMAN:
        return UtJob.NEEDS_HUMAN, f"人工修订仍未通过校验：{codes}", GateCode.HUMAN_REVISION_FAILED
    if not _row_tags(check) and not (gaps and gaps["open"]):
        # The model only adds input rows; nothing here can be closed that way.
        return UtJob.NEEDS_HUMAN, f"剩余问题无法通过新增输入行解决：{codes}", GateCode.NOT_ROW_FIXABLE
    if rounds >= max_rounds:
        return (UtJob.NEEDS_HUMAN, f"本地模型已修正 {rounds} 次仍未通过（上限 {max_rounds}）：{codes}",
                GateCode.ROUNDS_EXHAUSTED)
    return UtJob.REPAIRING, "", ""


def _host_case_context(attempt: UtAttempt, gaps: dict[str, Any]) -> bool:
    """Only identified missing-case work can use the Host input-row model."""
    check = attempt.check or {}
    witnesses = check.get("candidate_witnesses") or {}
    readiness = witnesses.get("input_repair_readiness") or {}
    if (not gaps["open"] or not gaps["report_complete"]
            or check.get("verdict") not in {"PASS", "FAIL"}
            or (attempt.execution or {}).get("status") != "HOST_PASS"
            or set(_error_codes(check)) - {"HOST_COVERAGE_REQUIRED_NOT_COVERED",
                                           "INPUT_OBLIGATION_UNRESOLVED",
                                           "INPUT_OBLIGATION_PROOF_GAP"}
            or any(issue.get("code") == "INPUT_OBLIGATION_UNRESOLVED"
                   and issue.get("severity") == "error" and issue.get("repairable") is not True
                   for issue in check.get("issues") or [])
            or any(issue.get("code") in {"HOST_COVERAGE_REQUIRED_UNKNOWN", "HOST_COVERAGE_UNAVAILABLE"}
                   for issue in check.get("issues") or [])
            or witnesses.get("contract") != "ut-agent.candidate-witness/1"
            or witnesses.get("candidate_sha256") != attempt.csv_sha256
            or check.get("candidate_sha256") != attempt.csv_sha256
            or witnesses.get("input_mapping_identified") is not True
            or readiness.get("ready") is not True
            or (witnesses.get("evidence") or {}).get("identified") is not True):
        return False
    try:
        table = repair.increment.parse(_read_attempt_csv(attempt).decode("cp932"))
        domains = (check.get("input_ranges") or {}).get("contract") or {}
        columns = domains.get("columns") or []
        return (domains.get("contract") == "ut-agent.input-domains/1"
                and len(columns) == len(table.inputs)
                and all(column.get("index") == index and column.get("column") == name
                        for index, (column, name) in enumerate(zip(columns, table.inputs), 1)))
    except (ServiceError, UnicodeDecodeError, ValueError):
        return False


def _host_next_state(job: UtJob, attempt: UtAttempt, gaps: dict[str, Any]) -> tuple[str, str, str]:
    check = attempt.check or {}
    if _host_case_context(attempt, gaps) and attempt.origin != UtAttempt.ORIGIN_HUMAN:
        if job.repair_rounds < job.max_repair_rounds:
            return UtJob.REPAIRING, "", ""
        return (UtJob.NEEDS_HUMAN, "Host missing cases remain; input repair budget exhausted" + _gap_note(gaps),
                GateCode.OPEN_GAPS_EXHAUSTED)
    if (check.get("verdict") == "PASS" and not gaps["open"]
            and not gaps.get("unproven", gaps["missing"] - gaps.get("witnessed", 0))):
        return (UtJob.AWAITING_REVIEW,
                "Clang/LLVM 校验通过，等待人工审核" + _gap_note(gaps),
                GateCode.MACHINE_PASS)
    if (check.get("verdict") in {"BLOCKED", "INVALID"}
            or (attempt.origin == UtAttempt.ORIGIN_HUMAN and check.get("verdict") == "FAIL")):
        state, reason, code = next_state(check, attempt.origin, job.repair_rounds, job.max_repair_rounds, gaps)
        detail = (attempt.execution or {}).get("reason")
        if detail and detail not in reason and code != GateCode.ENV_BLOCKED:
            reason = f"{reason}：{detail}" if reason else str(detail)
        return state, reason, code
    readiness = ((check.get("candidate_witnesses") or {}).get("input_repair_readiness") or {})
    if gaps["open"] and readiness.get("ready") is not True:
        return (UtJob.NEEDS_HUMAN,
                "Input repair capability is not proven: "
                + str(readiness.get("reason") or "Recheck typed input and invocation-slot bindings")
                + _gap_note(gaps), GateCode.CHECK_BLOCKED)
    reason = (attempt.execution or {}).get("reason") or "; ".join(
        str(issue.get("message", issue.get("code", ""))) for issue in check.get("issues", ()))
    return (UtJob.NEEDS_HUMAN, "Clang/LLVM 校验需人工处理：" + reason + _gap_note(gaps),
            GateCode.CHECK_BLOCKED)


def _host_gaps(job: UtJob, attempt: UtAttempt) -> dict[str, Any]:
    if any(item.get("gap_class") == _MISSING_CASE
           for item in diagnostic.report_records(attempt.check or {}, "unresolved")):
        return _job_gaps(job, attempt)
    return gap_status(attempt.check or {}, [], attempt.csv_sha256, attempt.execution)


def _execute_host_step(job: UtJob, attempt: UtAttempt, config: dict) -> None:
    """Drive the primary LLVM workflow using typed, version-bound evidence."""
    generation = job.generation or {}
    job.generation = {**generation, "host_step": "execute"}
    try:
        toolchains.verify_execution_toolchain(config, project_id=job.project_id)
    except ServiceError as exc:
        _gate(job, UtJob.ERROR, f"宿主机工具链校验失败：{exc}", GateCode.INTEGRITY_ERROR)
        db.session.commit()
        return
    try:
        _read_attempt_csv(attempt)
    except ServiceError as exc:
        _integrity_gate(job, attempt, "候选 CSV 完整性检查失败：" + str(exc))
        db.session.commit()
        return

    def persist(execution):
        attempt.execution = execution
        db.session.commit()

    def continuing():
        db.session.refresh(job, attribute_names=["stop_requested_at", "stop_requested_by"])
        return job.stop_requested_at is None

    execution_root = _fresh_exec_dir(job, attempt)
    try:
        with timing.stage(job, "host_workflow", attempt=attempt, kind="group"):
            execution, check = host.execute(
                bridge=get_bridge(), ir_json=generation.get("ir_json"),
                intents=generation.get("intent_manifest"), reference=job.reference_csv,
                candidate=attempt.csv_path, out=execution_root, config=config,
                model_timeout=current_app.config["UT_REPAIR_TIMEOUT"],
                persist=persist, continuing=continuing,
                observe=lambda step, **options: timing.stage(job, step, attempt=attempt, **options))
    except ProviderError as exc:
        _gate(job, UtJob.ERROR, f"本地模型不可用：{exc}", GateCode.MODEL_UNAVAILABLE)
    except BridgeError as exc:
        _gate(job, UtJob.ERROR, f"ut-agent 调用失败：{exc}", GateCode.TOOL_ERROR)
    else:
        if not _result_integrity(job, attempt, check, execution):
            db.session.commit()
            return
        attempt.execution, attempt.check, attempt.verdict = execution, check, check["verdict"]
        job.last_verdict = check["verdict"]
        evidence_gate = _report_gate(check, attempt.csv_sha256, execution)
        if not continuing():
            _halt(job)
        elif evidence_gate and (evidence_gate[2] != GateCode.REPORT_INCOMPLETE
                                or check["verdict"] in {"PASS", "FAIL"}):
            _gate(job, *evidence_gate)
        elif "typed_candidate" in execution:
            if evidence_gate:
                _gate(job, *evidence_gate)
                db.session.commit()
                return
            try:
                source = _read_attempt_csv(attempt)
                with timing.stage(job, "typed_candidate_processing", attempt=attempt):
                    derived, receipt = host_candidate.read_typed_candidate(
                        execution, source, Path(job.reference_csv).read_bytes(), execution_root)
                if _read_attempt_csv(attempt) != source or job.current_attempt_id != attempt.id:
                    raise ValueError("Candidate version changed while reading typed outputs")
            except (OSError, UnicodeError, ValueError, TypeError, ServiceError) as exc:
                _integrity_gate(job, attempt, "Host typed candidate rejected: " + str(exc), check, execution)
            else:
                _new_attempt(job, UtAttempt.ORIGIN_HOST_DERIVED, derived,
                             note="Appended outputs proven by the typed implementation oracle; re-execution required",
                             reviser={"oracle_source": "IMPLEMENTATION_DERIVED", "typed_candidate": receipt,
                                      "from_attempt": attempt.seq, "from_sha256": attempt.csv_sha256})
                job.last_verdict = ""
                _gate(job, UtJob.EXECUTING)
        else:
            _gate(job, *_host_next_state(job, attempt, _host_gaps(job, attempt)))
    if not continuing():
        _halt(job)
    db.session.commit()


def _job_gaps(job: UtJob, attempt: UtAttempt, *, through_seq: int | None = None) -> dict[str, Any]:
    """Gaps of `attempt` against the model rounds that shaped it.

    A staged proposal rejected before execution never became part of the
    lineage, so its claims and declines do not count; a round that added no
    rows (declines only) answered a lineage attempt and does.
    """
    attempts = {item.id: item for item in UtAttempt.query.filter_by(job_id=job.id)
                if through_seq is None or item.seq <= through_seq}
    lineage, cursor = set(), attempt.id
    while cursor in attempts and cursor not in lineage:
        lineage.add(cursor)
        cursor = (attempts[cursor].reviser or {}).get("parent_attempt_id")
    revisers = [item.reviser for item in attempts.values()
                if item.origin == UtAttempt.ORIGIN_LLM and (
                    item.id in lineage or (not item.csv_path and
                                           (item.reviser or {}).get("parent_attempt_id") in lineage))]
    return gap_status(attempt.check or {}, revisers, attempt.csv_sha256, attempt.execution)


def _expected_value_trace(attempt: UtAttempt, attempts: dict[int, UtAttempt]) -> dict[str, Any]:
    lineage, seen, cursor = [], set(), attempt
    complete = True
    while cursor is not None:
        if cursor.id in seen:
            complete = False
            break
        seen.add(cursor.id)
        lineage.append(cursor)
        parent = (cursor.reviser or {}).get("parent_attempt_id")
        if parent is None:
            break
        previous = attempts.get(parent)
        if previous is None or previous.job_id != attempt.job_id or previous.seq >= cursor.seq:
            complete = False
            break
        cursor = previous
    lineage.reverse()
    baseline = (lineage[0] if lineage and lineage[0].origin == UtAttempt.ORIGIN_GENERATOR
                and (lineage[0].reviser or {}).get("parent_attempt_id") is None else None)
    complete = complete and baseline is not None
    sources = []
    source = "UNKNOWN"
    for item in lineage:
        metadata = item.reviser or {}
        if "host_retry_of" in metadata:
            parent = attempts.get(metadata.get("parent_attempt_id"))
            if (parent is not None and metadata["host_retry_of"] == parent.id
                    and item.csv_sha256 == parent.csv_sha256):
                continue
            source, complete = "UNKNOWN", False
            continue
        if item.origin == UtAttempt.ORIGIN_GENERATOR:
            source = "GENERATOR"
        elif item.origin == UtAttempt.ORIGIN_HUMAN:
            source = "HUMAN"
        elif item.origin == UtAttempt.ORIGIN_HOST_DERIVED:
            receipt = metadata.get("typed_candidate") or {}
            parent = attempts.get(metadata.get("parent_attempt_id"))
            valid = (parent is not None
                     and metadata.get("oracle_source") == "IMPLEMENTATION_DERIVED"
                     and receipt.get("contract") == "ut-agent.host-typed-candidate/1"
                     and receipt.get("expected_value_source") == "IMPLEMENTATION_DERIVED"
                     and receipt.get("sha256") == item.csv_sha256
                     and receipt.get("source_candidate_sha256") == parent.csv_sha256
                     and metadata.get("from_sha256") == parent.csv_sha256)
            source = "IMPLEMENTATION_DERIVED" if valid else "UNKNOWN"
            complete = complete and valid
        elif item.origin != UtAttempt.ORIGIN_LLM:
            source, complete = "UNKNOWN", False
        if item.origin != UtAttempt.ORIGIN_LLM:
            sources.append({"attempt_id": item.id, "seq": item.seq, "origin": item.origin,
                            "csv_sha256": item.csv_sha256, "source": source,
                            "created_by": item.created_by})
    return {
        "expected_value_source": source if complete else "UNKNOWN",
        "expected_value_lineage": sources,
        "expected_value_lineage_complete": complete,
        "baseline": ({"attempt_id": baseline.id, "seq": baseline.seq,
                      "csv_sha256": baseline.csv_sha256, "source": "GENERATOR"} if baseline else None),
    }


def candidate_trace(job: UtJob, attempt: UtAttempt) -> dict[str, Any]:
    """Capture machine facts before a review can invalidate evidence."""
    check = attempt.check or {}
    report = diagnostic.gap_report(check)
    gaps = _job_gaps(job, attempt)
    unresolved = diagnostic.report_records(check, "unresolved")
    witnessed = _verified_obligation_ids(check, attempt.csv_sha256, attempt.execution)
    remaining = [item for item in unresolved
                 if item.get("gap_class") != _MISSING_CASE or item.get("obligation_id") not in witnessed
                 or (isinstance(item.get("requirement"), dict)
                     and item["requirement"].get("configuration_status") == "NEEDS_REVIEW")]
    pending = diagnostic.report_records(check, "output_pending")
    known = report["details_complete"] and report["output_pending_reported"]
    attempts = {item.id: item for item in UtAttempt.query.filter_by(job_id=job.id)
                if item.seq <= attempt.seq}
    return {
        "attempt_id": attempt.id, "csv_sha256": attempt.csv_sha256,
        "state": job.state, "gate_code": job.gate_code or "", "gate_reason": job.reason or "",
        "machine_verdict": job.last_verdict or "", "attempt_verdict": attempt.verdict or "",
        "open_gap_count": len(remaining) + len(pending) if known else None,
        "gap_counts": {
            "status": "COMPLETE" if known else "UNKNOWN",
            "details_complete": report["details_complete"],
            "reported_unresolved": report["reported_unresolved"],
            "supplied_unresolved": report["supplied_unresolved"],
            "reported_output_pending": report["reported_output_pending"],
            "supplied_output_pending": report["supplied_output_pending"],
            "output_pending_reported": report["output_pending_reported"],
            "remaining_supplied": len(remaining),
            "missing": gaps["missing"], "witnessed": gaps["witnessed"],
            "claimed": gaps["claimed"], "declined": gaps["declined"],
            "unproven": gaps["unproven"], "repair_open": len(gaps["open"]),
            "configuration_conflicts": gaps["configuration_conflicts"],
            "errors": report["errors"],
        },
        **_expected_value_trace(attempt, attempts),
    }




def _history_item(item: UtAttempt) -> dict[str, Any]:
    entry = {"seq": item.seq, "origin": item.origin, "verdict": item.verdict or "INVALID",
             "codes": _error_codes(item.check or {})}
    if item.origin == UtAttempt.ORIGIN_LLM and isinstance(item.reviser, dict):
        entry.update(added=len(item.reviser.get("rows") or ()),
                     declined=len(item.reviser.get("declined") or ()),
                     rejected=[rejected.get("error") for rejected in
                               (item.reviser.get("rejected") or ())[:3]])
        witnesses = (item.check or {}).get("candidate_witnesses") or {}
        entry["witness_feedback"] = [r for r in witnesses.get("results") or () if r.get("new")][:20]
        entry["proposed_inputs"] = item.reviser.get("rows") or []
        for key in ("error", "reply_head"):
            if item.reviser.get(key):
                entry[key] = str(item.reviser[key])[:2000]
        if item.reviser.get("schema_errors"):
            entry["schema_errors"] = item.reviser["schema_errors"][:20]
    return entry


def _repair_step(job: UtJob) -> None:
    attempt = db.session.get(UtAttempt, job.current_attempt_id)
    batch = db.session.get(UtBatch, job.batch_id)
    check = attempt.check or {}
    try:
        csv_bytes = _read_attempt_csv(attempt)
        errors = diagnostic.integrity_errors(check, attempt.csv_sha256, attempt.execution)
        if errors:
            raise ServiceError("; ".join(errors), code="INTEGRITY_ERROR")
    except ServiceError as exc:
        _integrity_gate(job, attempt, "自动修复前完整性检查失败：" + str(exc))
        db.session.commit()
        return
    evidence_gate = _report_gate(check, attempt.csv_sha256, attempt.execution)
    if evidence_gate:
        _gate(job, *evidence_gate)
        db.session.commit()
        return
    config = batch.config_snapshot or {}
    state = _host_next_state(job, attempt, _job_gaps(job, attempt))
    if state[0] != UtJob.REPAIRING:
        _gate(job, *state)
        db.session.commit()
        return
    job.generation = {**(job.generation or {}), "host_step": "repair"}
    history = [_history_item(item) for item in
               UtAttempt.query.filter_by(job_id=job.id).order_by(UtAttempt.seq)]
    gaps = _job_gaps(job, attempt)
    if not gaps["report_complete"]:
        _gate(job, UtJob.NEEDS_HUMAN, "测试义务明细不完整，请重新校验", GateCode.REPORT_INCOMPLETE)
        db.session.commit()
        return
    generation = dict(job.generation or {})
    constraints = {**(batch.constraints or {}), "execution_backend": "clang"}
    source = repair_source.load(generation.get("ir_json"), job.target.to_dict().get("function"))
    # Freeze the cacheable prefix in durable job metadata, including restarts.
    # A changed prompt version starts a new snapshot; same-version drift is
    # rejected by revise rather than silently replacing semantic evidence.
    prefix = generation.get("repair_prompt_prefix")
    if not prefix or prefix.get("version") != repair.PROMPT_VERSION:
        try:
            prefix = repair.prompt_prefix(constraints, job.target.to_dict(), csv_bytes, check, source)
        except (UnicodeDecodeError, repair.increment.TableError):
            prefix = None
        if prefix is not None:
            generation["repair_prompt_prefix"] = prefix
            job.generation = generation
            db.session.commit()
    try:
        revision = repair.revise(
            constraints=constraints,
            target=job.target.to_dict(),
            csv_bytes=csv_bytes,
            check=check,
            execution=attempt.execution,
            history=history,
            open_gaps=gaps["open"],
            allowed={item["obligation_id"] for item in gaps["open"]},
            model=(batch.config_snapshot or {}).get("repair_model", ""),
            timeout=current_app.config["UT_REPAIR_TIMEOUT"],
            prefix_snapshot=prefix,
            diagnostic_dir=_fresh_model_dir(job, attempt),
            source=source,
            observe=lambda step, **options: timing.stage(job, step, attempt=attempt,
                                                       round_number=job.repair_rounds + 1, **options),
        )
    except ProviderError as exc:
        # The model never answered: no round is consumed.
        _gate(job, UtJob.ERROR, f"本地模型不可用：{exc}", GateCode.MODEL_UNAVAILABLE)
        db.session.commit()
        return
    # The candidate may have changed while the model request was in flight.
    if not _result_integrity(job, attempt, check, attempt.execution):
        db.session.commit()
        return
    if revision.meta.get("code") == "INTEGRITY_ERROR":
        _integrity_gate(job, attempt, revision.meta["error"])
        db.session.commit()
        return
    if revision.meta.get("code") in {"PROMPT_PREFIX_CHANGED", "PROMPT_BUDGET_EXCEEDED",
                                   "DIAGNOSTIC_WRITE_FAILED"}:
        contacted = revision.meta.get("model_contacted") is True
        if contacted:
            job.repair_rounds += 1
        new = _new_attempt(job, UtAttempt.ORIGIN_LLM, None,
                           reviser={**revision.meta, "model_contacted": contacted},
                           note=revision.meta["error"], make_current=False)
        new.verdict = "BLOCKED"
        _gate(job, UtJob.NEEDS_HUMAN, new.note, revision.meta["code"])
        db.session.commit()
        return
    job.repair_rounds += 1
    new = _new_attempt(job, UtAttempt.ORIGIN_LLM, revision.csv_bytes,
                       reviser=revision.meta, note=revision.meta.get("error", ""), make_current=False)
    if revision.csv_bytes is not None:
        config = batch.config_snapshot or {}
        arguments = {
            "manifest": config["manifest_path"], "reference": Path(job.reference_csv) if job.reference_csv else None,
            "previous": Path(attempt.csv_path),
        }
        for key, argument in (("intent_manifest", "intents"), ("ir_json", "ir_json")):
            source = (job.generation or {}).get(key)
            if source and Path(source).is_file():
                arguments[argument] = Path(source)
        try:
            with timing.stage(job, "candidate_check", attempt=new, round_number=job.repair_rounds):
                verification = get_bridge().check(Path(new.csv_path), **arguments)
        except BridgeError as exc:
            new.verdict = "INVALID"
            new.note = f"候选检查失败：{exc}"
            _gate(job, UtJob.ERROR, new.note, GateCode.TOOL_ERROR)
            db.session.commit()
            return
        if not _result_integrity(job, new, verification, None):
            db.session.commit()
            return
        proposed = {row.get("obligation") for row in revision.meta.get("rows") or ()}
        missing = {gap["obligation_id"] for gap in gaps["open"]}
        if (verification.get("verdict") == "FORMAT_OK" and
                (proposed & missing) - _verified_obligation_ids(verification, new.csv_sha256)):
            verification = {**verification, "verdict": "BLOCKED", "issues": [
                *(verification.get("issues") or []),
                {"code": "INPUT_WITNESS_EVIDENCE_MISSING", "severity": "error", "repairable": False,
                 "message": "检查器未提供与本候选摘要绑定的输入义务证明，不能按模型认领接受候选"},
            ]}
        new.check = verification
        new.verdict = verification.get("verdict")
        if new.verdict == "FORMAT_OK":
            job.current_attempt_id = new.id
            _set_state(job, UtJob.EXECUTING)
        else:
            errors = ", ".join(_error_codes(verification))
            new.note = f"新增行在执行前被拒绝：{errors}；当前候选保持第 {attempt.seq} 版"
            if new.verdict == "BLOCKED" or job.repair_rounds >= job.max_repair_rounds:
                _gate(job, UtJob.NEEDS_HUMAN, new.note, GateCode.CANDIDATE_REJECTED)
        db.session.commit()
        return
    new.verdict = "INVALID"
    # Declines close only automatic work; required proof obligations remain.
    state, reason, code = _host_next_state(job, attempt, _job_gaps(job, attempt))
    if (state == UtJob.NEEDS_HUMAN and check.get("verdict") == "FAIL"
            and job.repair_rounds >= job.max_repair_rounds):
        reason = (f"本地模型已修正 {job.repair_rounds} 次仍未产出可用候选"
                  f"（上限 {job.max_repair_rounds}）")
        code = GateCode.ROUNDS_EXHAUSTED
    elif state == UtJob.NEEDS_HUMAN and revision.meta.get("error"):
        reason = f"{revision.meta['error']}；{reason}"
    elif state == UtJob.REPAIRING and _stalled(revision.meta):
        # The next round would see the same candidate, check and gaps: a
        # model that proposed nothing would only decline again (on real
        # data 10 of 11 rounds did, until the round limit ran out).
        state, code = UtJob.NEEDS_HUMAN, GateCode.MODEL_STALLED
        declined = len(revision.meta.get("declined") or ())
        reason = ("本地模型本轮没有提出任何新增行" +
                  (f"（声明无法补齐 {declined} 个）" if declined else "") +
                  "，输入没有变化，不再重复调用" +
                  (f"；模型说明：{revision.meta['rationale'][:200]}" if revision.meta.get("rationale") else ""))
    if state != UtJob.REPAIRING:
        _gate(job, state, reason, code)
    db.session.commit()


def _stalled(meta: dict[str, Any]) -> bool:
    """A parsed reply with no rows at all, not even rejected ones.

    Rejected rows come back as checker feedback the next round can act on,
    and an unparsable reply may be transient; neither is a stall.
    """
    return "rationale" in meta and not meta.get("rows") and not meta.get("rejected")


def advance_job(job_id: int) -> None:
    """Drive one job until it reaches a human gate."""
    batch_id = None
    while True:
        job = db.session.get(UtJob, job_id)
        if job is None:
            return
        batch_id = job.batch_id
        if job.state in UtJob.ACTIVE and job.stop_requested_at is not None:
            _halt(job)
            db.session.commit()
            break
        timing.driving(job)
        if job.state == UtJob.EXECUTING:
            _execute_step(job)
        elif job.state == UtJob.REPAIRING:
            _repair_step(job)
        else:
            break
    _refresh_batch(batch_id)


_INTERRUPTED_STEP = {UtJob.QUEUED: "排队", UtJob.GENERATING: "生成",
                     UtJob.EXECUTING: "执行与校验", UtJob.REPAIRING: "模型修订"}


def recover_interrupted(pending_jobs: set[int], pending_batches: set[int]) -> dict[str, list[int]]:
    """Route active jobs no worker will ever drive to a human gate.

    Call at worker start-up, before consumers run: an active job then has no
    live driver, and unless a queued task (``pending_*``) will pick it up,
    the worker that owned it died mid-step. The job is not re-run on its own
    -- a step that crashed the worker would crash it again on every restart
    -- but parked with its candidate and evidence for a person to retry.
    """
    result: dict[str, list[int]] = {"interrupted": [], "stopped": [], "pending": []}
    # A witness run only records RUNNING while its task holds the worker, so
    # anything still RUNNING here was orphaned by that worker.
    witness.clear_running()
    jobs = (UtJob.query.filter(UtJob.state.in_(UtJob.ACTIVE))
            .with_for_update(of=UtJob).populate_existing().all())
    failed_batches: set[int] = set()
    for job in jobs:
        before_generation = job.state in (UtJob.QUEUED, UtJob.GENERATING)
        if ((job.state in (UtJob.EXECUTING, UtJob.REPAIRING) and job.id in pending_jobs)
                or (job.state == UtJob.QUEUED and job.batch_id in pending_batches)):
            timing.interrupt(job, preserve_waits=True)
            timing.transition(job, job.state)
            result["pending"].append(job.id)
            continue
        timing.interrupt(job)
        if job.stop_requested_at is not None:
            _halt(job)
            result["stopped"].append(job.id)
        elif before_generation or job.current_attempt_id is None:
            _gate(job, UtJob.ERROR,
                  f"worker 在{_INTERRUPTED_STEP[job.state]}阶段中断，尚无可继续的候选；请在「任务/运行」重新入队",
                  GateCode.INTERRUPTED)
            failed_batches.add(job.batch_id)
            result["interrupted"].append(job.id)
        else:
            _gate(job, UtJob.ERROR,
                  f"worker 在{_INTERRUPTED_STEP[job.state]}阶段中断；当前版本与已有证据均已保留，"
                  "确认 worker 正常后点「重试」继续",
                  GateCode.INTERRUPTED)
            result["interrupted"].append(job.id)
        audit.record("ut.job.interrupted", actor_id=None, object_type="ut_job", object_id=job.id,
                     project_id=job.project_id, new_value={"state": job.state, "gate_code": job.gate_code})
    for batch in UtBatch.query.filter(UtBatch.id.in_(failed_batches),
                                      UtBatch.status == UtBatch.STATUS_GENERATING):
        batch.status = UtBatch.STATUS_FAILED
        batch.error = "worker 在生成阶段中断"
        batch.finished_at = _utcnow()
    db.session.commit()
    for batch_id in {job.batch_id for job in jobs}:
        _refresh_batch(batch_id)
    return result


# --------------------------------------------------------------------------- #
# Human actions
# --------------------------------------------------------------------------- #
def _row_lock(project_id: int, job_id: int) -> UtJob:
    job = (UtJob.query.filter_by(id=job_id, project_id=project_id)
           .with_for_update(of=UtJob).populate_existing().first())
    if job is None:
        raise ServiceError("作业不存在", code="NOT_FOUND")
    return job


def _locked_job(project_id: int, job_id: int) -> UtJob:
    job = _row_lock(project_id, job_id)
    if job.state not in UtJob.ACTIONABLE:
        raise ServiceError(f"作业当前状态为 {job.state}，不接受该操作", code="INVALID_STATE")
    if job.superseded_by:
        # Acting on an older candidate would sign off or rerun work the
        # requester already replaced by queueing the function again.
        raise ServiceError(f"该函数已有更新的作业 #{job.superseded_by}，旧作业只读",
                           code="SUPERSEDED")
    return job


def _halt(job: UtJob) -> None:
    """Park an active job the user asked to stop; candidate and evidence stay."""
    _set_state(job, UtJob.STOPPED)
    job.reason = "已按请求停止；当前候选、机器判定与修订记录均已保留，可继续或重新入队"
    job.gate_code = GateCode.STOPPED


def _resume(job: UtJob) -> None:
    job.stop_requested_at = None
    job.stop_requested_by = None


def stop_job(project_id: int, job_id: int, user_id: int) -> UtJob:
    """Stop one active job.

    A queued job has no worker yet and stops at once.  A running one finishes
    its current external call (the LLVM run or model reply is evidence worth
    keeping) and stops at the next step boundary.
    """
    job = _row_lock(project_id, job_id)
    if job.state not in UtJob.ACTIVE:
        raise ServiceError(f"作业当前状态为 {job.state}，没有可停止的运行", code="INVALID_STATE")
    _request_stop(job, user_id)
    db.session.commit()
    return job


def stop_batch(project_id: int, batch_id: int, user_id: int) -> dict[str, int]:
    batch = UtBatch.query.filter_by(id=batch_id, project_id=project_id).first()
    if batch is None:
        raise ServiceError("批次不存在", code="NOT_FOUND")
    jobs = (UtJob.query.filter(UtJob.batch_id == batch_id, UtJob.state.in_(UtJob.ACTIVE))
            .with_for_update(of=UtJob).populate_existing().all())
    if not jobs:
        raise ServiceError("该批次没有运行中的作业", code="INVALID_STATE")
    stopped = sum(_request_stop(job, user_id, batch_wide=True) for job in jobs)
    db.session.commit()
    return {"requested": len(jobs), "stopped": stopped, "stopping": len(jobs) - stopped}


def _request_stop(job: UtJob, user_id: int, *, batch_wide: bool = False) -> int:
    """Return 1 when the job stopped now, 0 when its worker will stop it."""
    if job.stop_requested_at is None:
        job.stop_requested_at = _utcnow()
        job.stop_requested_by = user_id
        audit.record("ut.job.stop", actor_id=user_id, object_type="ut_job", object_id=job.id,
                     project_id=job.project_id, old_value={"state": job.state},
                     new_value={"batch_id": job.batch_id, "batch_wide": batch_wide})
    if job.state == UtJob.QUEUED:
        _halt(job)
        return 1
    return 0


def submit_revision(project_id: int, job_id: int, csv_bytes: bytes, user_id: int,
                    note: str = "") -> UtJob:
    if not csv_bytes:
        raise ServiceError("CSV 不能为空", code="VALIDATION_ERROR")
    job = _locked_job(project_id, job_id)
    _resume(job)
    _new_attempt(job, UtAttempt.ORIGIN_HUMAN, csv_bytes, note=note, user_id=user_id)
    _gate(job, UtJob.EXECUTING)
    job.last_verdict = ""
    audit.record("ut.job.revise", actor_id=user_id, object_type="ut_job", object_id=job.id,
                 project_id=project_id,
                 new_value={"attempt_id": job.current_attempt_id, "note": note})
    db.session.commit()
    return job


def review(project_id: int, job_id: int, decision: str, user_id: int, note: str = "",
           *, expected_attempt_id: Any = _EXPECTED_ATTEMPT_UNSET) -> UtJob:
    note = (note or "").strip()
    if decision not in ("approve", "reject"):
        raise ServiceError("decision 必须是 approve 或 reject", code="VALIDATION_ERROR")
    job = _locked_job(project_id, job_id)
    # Omission preserves the existing API contract. Explicit null means the
    # caller saw no candidate and must not sign off a subsequently created one.
    if (expected_attempt_id is not _EXPECTED_ATTEMPT_UNSET
            and expected_attempt_id != job.current_attempt_id):
        raise VersionConflict(expected_attempt_id or 0, job.current_attempt_id or 0, job.to_dict())
    attempt = db.session.get(UtAttempt, job.current_attempt_id) if job.current_attempt_id else None
    evidence_gate = None
    if decision == "approve":
        if job.current_attempt_id is None:
            raise ServiceError("没有可通过的候选 CSV", code="INVALID_STATE")
        if attempt is None or not attempt.csv_path:
            raise ServiceError("没有可通过的候选 CSV", code="INVALID_STATE")
        try:
            _read_attempt_csv(attempt)
        except ServiceError as exc:
            raise ServiceError("审核时候选完整性检查失败：" + str(exc) +
                               "；请恢复原版本文件或提交新的修订后重新校验", code="INTEGRITY_ERROR") from exc
        if attempt.origin == UtAttempt.ORIGIN_HOST_DERIVED:
            checked, executed = attempt.check or {}, attempt.execution or {}
            binding_sha = ((executed.get("input_binding") or {}).get("candidate") or {}).get("sha256")
            if (checked.get("executed") is not True or checked.get("candidate_sha256") != attempt.csv_sha256
                    or executed.get("evidence_source") != "HOST_EXECUTION" or binding_sha != attempt.csv_sha256):
                raise ServiceError("Typed Host outputs require execution of this candidate version before approval",
                                   code="INTEGRITY_ERROR")
        evidence_gate = _report_gate(attempt.check or {}, attempt.csv_sha256, attempt.execution)
        if (job.state != UtJob.AWAITING_REVIEW or evidence_gate or
                job.gate_code in {GateCode.INTEGRITY_ERROR, GateCode.CONFIGURATION_CONFLICT, GateCode.REPORT_INCOMPLETE}) and not note:
            raise ServiceError("机器校验未通过的候选需要填写审核意见才能通过", code="VALIDATION_ERROR")
    elif not note:
        raise ServiceError("驳回必须填写原因", code="VALIDATION_ERROR")
    machine = candidate_trace(job, attempt) if attempt else {
        "attempt_id": None, "state": job.state, "gate_code": job.gate_code or "",
        "gate_reason": job.reason or "", "machine_verdict": job.last_verdict or "",
    }
    if decision == "approve":
        if evidence_gate and evidence_gate[2] == GateCode.INTEGRITY_ERROR:
            attempt.check = _integrity_check(attempt.check or {}, evidence_gate[1])
            attempt.verdict = job.last_verdict = "BLOCKED"
        _set_state(job, UtJob.APPROVED)
    else:
        _set_state(job, UtJob.REJECTED)
    job.reviewed_by = user_id
    job.reviewed_at = _utcnow()
    job.review_note = note
    # Keep the machine gate. Approval records a human decision; it does not
    # erase why the candidate stopped or which obligations were still open.
    approved = job.state == UtJob.APPROVED
    trace = {"machine": machine, "decision": decision, "reviewed_by": user_id,
             "reviewed_at": _iso(job.reviewed_at), "note": note,
             "human_override": approved and (
                 machine["state"] != UtJob.AWAITING_REVIEW
                 or machine["gate_code"] != GateCode.MACHINE_PASS
                 or machine["machine_verdict"] != "PASS" or evidence_gate is not None),
             "review_gate": ({"code": evidence_gate[2], "reason": evidence_gate[1]}
                             if evidence_gate else None)}
    job.generation = {**(job.generation or {}), "review_trace": deepcopy(trace)}
    audit.record("ut.job.approve" if approved else "ut.job.reject", actor_id=user_id,
                 object_type="ut_job", object_id=job.id, project_id=project_id,
                 old_value=machine,
                 new_value={"attempt_id": job.current_attempt_id,
                            "verdict": job.last_verdict or None, "note": note,
                            "review_trace": trace})
    _notify_requester(job, notify_svc.REVIEW_APPROVED if approved else notify_svc.REVIEW_REJECTED,
                      "单元测试审核通过" if approved else "单元测试审核驳回", note, actor_id=user_id)
    db.session.commit()
    return job


def retry(project_id: int, job_id: int, user_id: Optional[int] = None) -> UtJob:
    job = _locked_job(project_id, job_id)
    if job.state not in (UtJob.BLOCKED, UtJob.ERROR, UtJob.STOPPED):
        raise ServiceError("只有 blocked / error / stopped 状态的作业可以重试", code="INVALID_STATE")
    attempt = db.session.get(UtAttempt, job.current_attempt_id) if job.current_attempt_id else None
    if attempt is None:
        raise ServiceError("生成阶段失败或停止的作业请重新加入队列", code="INVALID_STATE")
    _resume(job)
    audit.record("ut.job.retry", actor_id=user_id, object_type="ut_job", object_id=job.id,
                 project_id=project_id, old_value={"state": job.state, "reason": job.reason})
    # An error during repair leaves a checked attempt that still wants rows
    # (FAIL, or PASS with open missing cases): resume the repair.
    config = db.session.get(UtBatch, job.batch_id).config_snapshot or {}
    host_repair = ((job.generation or {}).get("host_step") == "repair"
                   and _report_gate(attempt.check or {}, attempt.csv_sha256, attempt.execution) is None
                   and _host_case_context(attempt, _job_gaps(job, attempt)))
    if config.get("stub_mode") == "llm" and not host_repair:
        # A fresh model reply changes the reviewed candidate even when CSV bytes
        # stay identical or preparation failed before evidence was captured.
        # Preserve the previous attempt and stale-review fence in every case.
        _new_attempt(job, attempt.origin, _read_attempt_csv(attempt),
                     note="Host stub retry; CSV unchanged, candidate evidence version renewed",
                     reviser={"host_retry_of": attempt.id}, user_id=user_id)
    _gate(job, UtJob.REPAIRING if host_repair else UtJob.EXECUTING)
    db.session.commit()
    return job


def dispatch_failed(project_id: int, exc: Exception, *, job_id: int | None = None,
                    batch_id: int | None = None) -> None:
    """Gate work whose background task could not be enqueued.

    The state commit and the queue write are separate transactions. Without a
    task nothing advances the active job, and retry/requeue refuse active jobs,
    so the work would stay active until the next worker restart.
    """
    db.session.rollback()
    reason = "投递后台任务失败：" + str(exc)
    if batch_id is not None:
        batch = (UtBatch.query.filter_by(id=batch_id, project_id=project_id)
                 .with_for_update().populate_existing().one())
        jobs = (UtJob.query.filter_by(batch_id=batch_id, state=UtJob.QUEUED)
                .with_for_update(of=UtJob).populate_existing().all())
        if batch.status == UtBatch.STATUS_GENERATING:
            batch.status = UtBatch.STATUS_FAILED
            batch.error = reason
            batch.finished_at = _utcnow()
    else:
        job = _row_lock(project_id, job_id)
        jobs = [job] if job.state in (UtJob.EXECUTING, UtJob.REPAIRING) else []
    for job in jobs:
        if job.stop_requested_at is not None:
            _halt(job)
        else:
            _gate(job, UtJob.ERROR, reason, GateCode.INTERNAL_ERROR)
    db.session.commit()


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #
def list_jobs(project_id: int, *, state: Optional[str] = None,
              batch_id: Optional[int] = None) -> list[dict[str, Any]]:
    query = UtJob.query.filter_by(project_id=project_id)
    if state:
        query = query.filter(UtJob.state == state)
    if batch_id:
        query = query.filter(UtJob.batch_id == batch_id)
    jobs = query.order_by(UtJob.id.desc()).limit(_JOB_LIST_LIMIT).all()
    digests = _host_coverage_digests([job.current_attempt_id for job in jobs if job.current_attempt_id])
    entries = []
    for job in jobs:
        entry = job.to_dict()
        entry["coverage"] = digests.get(job.current_attempt_id)
        entries.append(entry)
    return entries


_COVERAGE_LINES_CONTRACT = "ut-agent.coverage-lines/1"


def _percent(count: Any) -> Optional[float]:
    return count.get("percent") if isinstance(count, dict) else None


def _host_coverage_digests(attempt_ids: list[int]) -> dict[int, dict[str, Any]]:
    """C0/C1/MC/DC percentages of each current attempt's host llvm-cov run.

    Only HOST_EXECUTION evidence is summarised (the platform does not process
    WinAMS coverage), and only the small JSON paths are read, never the
    attempt's whole execution document.
    """
    if not attempt_ids:
        return {}
    rows = (db.session.query(UtAttempt.id, UtAttempt.execution["evidence_source"],
                             UtAttempt.execution["coverage"],
                             UtAttempt.execution["coverage_report"]["contract"])
            .filter(UtAttempt.id.in_(attempt_ids)).all())
    digests = {}
    for attempt_id, source, coverage, contract in rows:
        if source != "HOST_EXECUTION" or not isinstance(coverage, dict) or coverage.get("status") != "MEASURED":
            continue
        digests[attempt_id] = {
            "evidence_source": source,
            "c0": _percent(coverage.get("lines")),
            "c1": _percent(coverage.get("c1")),
            "mcdc": _percent(coverage.get("mcdc")),
            "has_report": contract == _COVERAGE_LINES_CONTRACT,
        }
    return digests


def _round_outcome(attempt: UtAttempt) -> str:
    """One model round, from what it left behind (works for older records too)."""
    meta = attempt.reviser if isinstance(attempt.reviser, dict) else {}
    if meta.get("model_contacted") is False:
        return "guarded"              # prompt guard stopped it before the model
    if attempt.csv_path:
        if attempt.execution is not None:
            return "passed" if attempt.verdict == "PASS" else "executed"
        return "refused"              # rows appended but refused before execution
    if "rationale" not in meta:
        return "unusable"             # reply was not usable JSON
    return "rejected" if meta.get("rejected") else "stalled"


def repair_stats(project_id: int) -> dict[str, Any]:
    """How much the local model's rounds actually produced, per model.

    A round counts once the model was asked. ``useful`` is a candidate that
    reached LLVM execution; ``stalled`` proposed nothing at all, ``rejected`` only
    rows the validator refused.
    """
    attempts = (UtAttempt.query.join(UtJob, UtJob.id == UtAttempt.job_id)
                .filter(UtJob.project_id == project_id, UtAttempt.origin == UtAttempt.ORIGIN_LLM)
                .all())
    models: dict[str, dict[str, Any]] = {}
    for attempt in attempts:
        meta = attempt.reviser if isinstance(attempt.reviser, dict) else {}
        entry = models.setdefault(meta.get("model") or "未记录", {
            "rounds": 0, "guarded": 0, "passed": 0, "executed": 0, "refused": 0,
            "rejected": 0, "stalled": 0, "unusable": 0, "jobs": set(),
            "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
            "elapsed_ms": [],
        })
        outcome = _round_outcome(attempt)
        entry[outcome] += 1
        if outcome == "guarded":
            continue
        entry["rounds"] += 1
        entry["jobs"].add(attempt.job_id)
        usage = meta.get("usage") if isinstance(meta.get("usage"), dict) else {}
        for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
            entry[key] += int(usage.get(key) or 0)
        if usage.get("elapsed_ms"):
            entry["elapsed_ms"].append(int(usage["elapsed_ms"]))
    rows = []
    for model, entry in sorted(models.items()):
        rounds, elapsed = entry["rounds"], entry.pop("elapsed_ms")
        entry["jobs"] = len(entry["jobs"])
        entry.update(
            model=model,
            useful=entry["passed"] + entry["executed"],
            avg_input_tokens=round(entry["input_tokens"] / rounds) if rounds else 0,
            cache_ratio=(round(entry["cached_input_tokens"] / entry["input_tokens"], 3)
                         if entry["input_tokens"] else None),
            avg_elapsed_ms=round(sum(elapsed) / len(elapsed)) if elapsed else None,
        )
        rows.append(entry)
    return {"models": rows}


_PROGRESS_SCHEMA = "ut-agent.generation-progress/1"
_C_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _read_progress(path: Path, schema: str = _PROGRESS_SCHEMA) -> Optional[dict[str, Any]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict) or document.get("schema") != schema:
        return None
    return document


def _generation_progress(job: UtJob) -> dict[str, Any]:
    """Live snapshots ut-agent writes while it generates (observation only).

    Missing snapshots mean generation has not reached that point yet, e.g. the
    batch still waits for the process-wide generation gate.
    """
    root = _generation_progress_dir(job.project_id, job.batch_id)
    function = job.target.function if job.target else ""
    return {
        "observed_at": _iso(_utcnow()),
        "project": _read_progress(root / "__project__.json"),
        "function": (_read_progress(root / f"{function}.json")
                     if _C_IDENTIFIER.fullmatch(function) else None),
        # Advisory only: whether this batch looks like it will exhaust its
        # semantic step budget before it finishes.  Never gates anything.
        "forecast": _read_progress(root / "forecast.json", _FORECAST_SCHEMA),
    }


def job_detail(project_id: int, job_id: int) -> dict[str, Any]:
    job = UtJob.query.filter_by(id=job_id, project_id=project_id).first()
    if job is None:
        raise ServiceError("作业不存在", code="NOT_FOUND")
    attempts = UtAttempt.query.filter_by(job_id=job.id).order_by(UtAttempt.seq).all()
    entry = job.to_dict(attempts=attempts)
    entry["batch"] = db.session.get(UtBatch, job.batch_id).to_dict()
    progress = _generation_progress(job)
    if job.state == UtJob.GENERATING:
        entry["generation_progress"] = progress
    entry["timing"] = timing.snapshot(job, progress)
    return entry


def attempt_csv(project_id: int, job_id: int, attempt_id: int) -> tuple[bytes, str]:
    attempt = _csv_attempt(project_id, job_id, attempt_id)
    data = _read_attempt_csv(attempt)
    job = db.session.get(UtJob, job_id)
    function = job.target.function
    if not _C_IDENTIFIER.fullmatch(function):
        raise ServiceError("Invalid target function name", code="INTEGRITY_ERROR")
    return data, f"{function}.csv"


def _csv_attempt(project_id: int, job_id: int, attempt_id: int) -> UtAttempt:
    attempt = (UtAttempt.query.join(UtJob, UtJob.id == UtAttempt.job_id)
               .filter(UtAttempt.id == attempt_id, UtJob.id == job_id,
                       UtJob.project_id == project_id).first())
    if attempt is None or not attempt.csv_path:
        raise ServiceError("该版本没有 CSV", code="NOT_FOUND")
    return attempt


def _read_attempt_csv(attempt: UtAttempt) -> bytes:
    try:
        data = Path(attempt.csv_path).read_bytes()
    except OSError as exc:
        raise ServiceError("Candidate CSV is missing", code="NOT_FOUND") from exc
    if not attempt.csv_sha256 or hashlib.sha256(data).hexdigest() != attempt.csv_sha256:
        raise ServiceError("Candidate CSV changed after capture", code="INTEGRITY_ERROR")
    return data


def attempt_diagnostic(project_id: int, job_id: int, attempt_id: int) -> dict[str, Any]:
    attempt = _csv_attempt(project_id, job_id, attempt_id)
    data = _read_attempt_csv(attempt)
    job = db.session.get(UtJob, job_id)
    batch = db.session.get(UtBatch, job.batch_id)
    check = attempt.check or {}
    # Historical exports use only that candidate's lineage, as of its version.
    gaps = _job_gaps(job, attempt, through_seq=attempt.seq)
    allowed = {g["obligation_id"] for g in gaps["open"]}
    try:
        table = repair.increment.parse(data.decode("cp932"))
        return diagnostic.build_packet(
            project_id=project_id, job_id=job_id, attempt=attempt.to_dict(),
            target=job.target.to_dict(), csv_bytes=data, constraints=batch.constraints or {},
            provenance=batch.provenance or {}, allowed=allowed,
            editable_columns=repair._editable_columns(table, check))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ServiceError(str(exc), code="INTEGRITY_ERROR") from exc


def attempt_coverage_report(project_id: int, job_id: int, attempt_id: int) -> dict[str, Any]:
    """The per-line C0/C1/MC/DC report of one attempt's host llvm-cov run.

    Development-time host evidence only; it never stands for target coverage.
    """
    attempt = (UtAttempt.query.join(UtJob, UtJob.id == UtAttempt.job_id)
               .filter(UtAttempt.id == attempt_id, UtJob.id == job_id,
                       UtJob.project_id == project_id).first())
    report = (attempt.execution or {}).get("coverage_report") if attempt else None
    if (not isinstance(report, dict) or report.get("contract") != _COVERAGE_LINES_CONTRACT
            or report.get("evidence_source") != "HOST_EXECUTION"
            or not isinstance(report.get("lines"), list)):
        raise ServiceError("该版本没有主机覆盖率报告", code="NOT_FOUND")
    return {"attempt": {"id": attempt.id, "seq": attempt.seq, "verdict": attempt.verdict or None},
            "report": report}


def attempt_artifact(project_id: int, job_id: int, attempt_id: int, artifact_id: str) -> tuple[bytes, str]:
    attempt = (UtAttempt.query.join(UtJob, UtJob.id == UtAttempt.job_id)
               .filter(UtAttempt.id == attempt_id, UtJob.id == job_id,
                       UtJob.project_id == project_id).first())
    if attempt is None:
        raise ServiceError("Artifact not found", code="NOT_FOUND")
    sources = [(attempt.execution or {}).get("stub_generation") or {}, attempt.reviser or {}]
    match = next(((metadata, item) for metadata in sources
                  for item in metadata.get("artifacts", ()) if item.get("id") == artifact_id), None)
    if match is None:
        raise ServiceError("Artifact not found", code="NOT_FOUND")
    metadata, artifact = match
    root = metadata.get("artifact_root")
    if artifact is None or not root:
        raise ServiceError("Artifact not found", code="NOT_FOUND")
    job = db.session.get(UtJob, job_id)
    base = Path(root).resolve()
    path = (base / artifact["name"]).resolve()
    if not base.is_relative_to(_job_dir(job).resolve()) or not path.is_relative_to(base):
        raise ServiceError("Artifact location is invalid", code="INTEGRITY_ERROR")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ServiceError("Artifact is missing", code="NOT_FOUND") from exc
    if hashlib.sha256(data).hexdigest() != artifact.get("sha256"):
        raise ServiceError("Artifact changed after capture", code="INTEGRITY_ERROR")
    return data, path.name
