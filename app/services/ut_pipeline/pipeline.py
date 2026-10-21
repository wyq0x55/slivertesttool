"""Project binding, 実施一覧 import, batches and the per-target job state machine.

::

    queued → generating ─┬─ generator TestCsv ─────────────→ executing
                         └─ no TestCsv / bridge failure ───→ needs_human | error
    executing  (ut-agent check; WinAMS runs only after the static check passes)
        PASS                               → awaiting_review
        PASS with open missing cases,
          LLM rounds left                  → repairing → executing → ...
        FAIL, LLM rounds left              → repairing → executing → ...
        FAIL that input rows cannot close  → needs_human
        FAIL, rounds exhausted             → needs_human
        FAIL on a human revision           → needs_human
        BLOCKED by the WinAMS environment  → blocked
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

import hashlib
import threading
from pathlib import Path
from typing import Any, Optional

from flask import current_app
from sqlalchemy import func

from ...extensions import db
from ...models import UtAttempt, UtBatch, UtJob, UtPipelineConfig, UtTarget
from ...models.ut_pipeline import GateCode
from ...models.lanmatrix import _utcnow
from ..ai.provider import ProviderError
from ..lanmatrix import audit
from ..lanmatrix import notification_service as notify_svc
from ..lanmatrix.errors import ServiceError, VersionConflict
from . import host, repair
from .bridge import BridgeError, BridgeSettings, UtAgentBridge

# The worker is one process with a thread pool.  WinAMS runs and project-wide
# extraction are serialised; checks and LLM revisions stay concurrent.
_WINAMS_GATE = threading.Lock()
_GENERATION_GATE = threading.Lock()
_EXPECTED_ATTEMPT_UNSET = object()

_CONFIG_TEXT_FIELDS = ("manifest_path", "index_csv", "product_root", "work_root",
                       "stub_builds", "repair_model", "execution_backend", "stub_mode",
                       "stub_model", "host_clang", "host_llvm_cov", "host_llvm_profdata",
                       "host_profile_runtime")
_REQUIRED_FIELDS = ("manifest_path", "index_csv", "product_root")
_JOB_LIST_LIMIT = 500


def get_bridge() -> UtAgentBridge:
    return UtAgentBridge(BridgeSettings.from_config(current_app.config))


def _lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# Project binding
# --------------------------------------------------------------------------- #
def get_config(project_id: int) -> Optional[UtPipelineConfig]:
    return UtPipelineConfig.query.filter_by(project_id=project_id).first()


def require_config(project_id: int) -> UtPipelineConfig:
    config = get_config(project_id)
    required = _REQUIRED_FIELDS + (("work_root",) if config is None or config.execution_backend == "winams" else ())
    if config is None or not all(getattr(config, key) for key in required):
        raise ServiceError(
            "请先配置 ut-agent 项目绑定（ProjectManifest / 索引 CSV / Product 根；WinAMS 还需工作根）",
            code="UT_NOT_CONFIGURED")
    return config


def save_config(project_id: int, values: dict[str, Any], user_id: int) -> UtPipelineConfig:
    existing = get_config(project_id)
    before = existing.to_dict() if existing else None
    normalized = {}
    for key in _CONFIG_TEXT_FIELDS:
        if key in values:
            if not isinstance(values[key], str):
                raise ServiceError(f"{key} must be a string", code="VALIDATION_ERROR")
            value = values[key].strip()
            limit = 128 if key in ("stub_model", "repair_model") else 1024
            if key != "stub_builds" and len(value) > limit:
                raise ServiceError(f"{key} exceeds {limit} characters", code="VALIDATION_ERROR")
            normalized[key] = "\n".join(_lines(value)) if key == "stub_builds" else value
    for key, default, allowed in (("execution_backend", "winams", ("winams", "clang")),
                                  ("stub_mode", "deterministic", ("deterministic", "llm"))):
        effective = normalized.get(key, getattr(existing, key, default))
        if effective not in allowed:
            raise ServiceError(f"{key} must be one of {', '.join(allowed)}", code="VALIDATION_ERROR")
        normalized[key] = effective
    if normalized["stub_mode"] == "llm" and normalized["execution_backend"] != "clang":
        raise ServiceError("LLM stubs require the clang execution backend", code="VALIDATION_ERROR")
    for key in ("host_clang", "host_llvm_cov", "host_llvm_profdata"):
        if key in normalized and not normalized[key]:
            raise ServiceError(f"{key} must not be empty", code="VALIDATION_ERROR")
    for key, low, high in (("max_repair_rounds", 0, 20), ("stub_max_rounds", 1, 5)):
        if key in values:
            value = values[key]
            if type(value) is not int or not low <= value <= high:
                raise ServiceError(f"{key} must be an integer from {low} to {high}", code="VALIDATION_ERROR")
            normalized[key] = value
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
            "execution_backend": (job.generation or {}).get("execution_backend", "winams")}


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
def create_batch(project_id: int, target_ids: Any, user_id: int, label: str = "") -> UtBatch:
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
                    status=UtBatch.STATUS_GENERATING, config_snapshot=config.to_dict())
    db.session.add(batch)
    db.session.flush()
    for target in sorted(targets, key=lambda item: item.row_no):
        job = UtJob(batch_id=batch.id, project_id=project_id, target_id=target.id,
                    max_repair_rounds=config.max_repair_rounds)
        db.session.add(job)
        db.session.flush()
        target.latest_job_id = job.id
    audit.record("ut.batch.create", actor_id=user_id, object_type="ut_batch",
                 object_id=batch.id, project_id=project_id,
                 new_value={"functions": [t.function for t in targets], "label": batch.label})
    db.session.commit()
    return batch


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


def _job_dir(job: UtJob) -> Path:
    return _batch_dir(job.project_id, job.batch_id) / f"j{job.id}"


def _gate(job: UtJob, state: str, reason: str = "", code: str = "") -> None:
    reached_gate = state in UtJob.HUMAN_GATES and job.state != state
    job.state = state
    job.reason = reason
    job.gate_code = code if state in UtJob.ACTIONABLE else ""
    if reached_gate:
        _notify_requester(job, notify_svc.TASK_FINISHED, _GATE_TITLES[state], reason)


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
def run_generation(batch_id: int) -> list[int]:
    """Generate every queued job of ``batch_id``; return the jobs ready to execute."""
    batch = db.session.get(UtBatch, batch_id)
    if batch is None or batch.status != UtBatch.STATUS_GENERATING:
        return []
    jobs = UtJob.query.filter_by(batch_id=batch_id, state=UtJob.QUEUED).all()
    if not jobs:
        # Every job was stopped before generation started.
        batch.status = UtBatch.STATUS_DONE
        batch.finished_at = _utcnow()
        db.session.commit()
        return []
    for job in jobs:
        job.state = UtJob.GENERATING
    db.session.commit()
    config = batch.config_snapshot or {}
    bridge = get_bridge()
    try:
        constraints = bridge.constraints(config["manifest_path"])
        with _GENERATION_GATE:
            document = bridge.generate(
                config["manifest_path"], config["index_csv"], config["product_root"],
                _batch_dir(batch.project_id, batch.id) / "generation",
                [job.target.function for job in jobs],
            )
    except BridgeError as exc:
        batch.status = UtBatch.STATUS_FAILED
        batch.error = str(exc)
        batch.finished_at = _utcnow()
        for job in jobs:
            _gate(job, UtJob.ERROR, f"生成失败：{exc}", GateCode.GENERATION_FAILED)
        db.session.commit()
        return []
    batch.constraints = constraints
    batch.provenance = document.get("provenance")
    units = {unit["function"]: unit for unit in document.get("units", ())}
    ready = []
    for job in jobs:
        unit = units.get(job.target.function)
        if unit is None:
            _gate(job, UtJob.ERROR, "生成结果中缺少该函数", GateCode.GENERATION_FAILED)
            continue
        job.generation_status = str(unit.get("status") or "")
        job.generation = {**unit, "execution_backend": config.get("execution_backend", "winams")}
        if not unit.get("testcsv"):
            detail = unit.get("error") or unit.get("projection_failure") or ""
            _gate(job, UtJob.NEEDS_HUMAN,
                  f"生成器未产出 TestCsv（{job.generation_status}）：{detail}", GateCode.NO_TESTCSV)
            continue
        attempt = _new_attempt(job, UtAttempt.ORIGIN_GENERATOR,
                               Path(unit["testcsv"]).read_bytes(),
                               note=f"generation status {job.generation_status}")
        # The generator CSV is the baseline reference every revision must keep.
        job.reference_csv = attempt.csv_path
        if job.stop_requested_at is not None:
            _halt(job)
            continue
        job.state = UtJob.EXECUTING
        ready.append(job.id)
    batch.status = UtBatch.STATUS_RUNNING
    db.session.commit()
    _refresh_batch(batch.id)
    return ready


# --------------------------------------------------------------------------- #
# Execution / check / repair loop
# --------------------------------------------------------------------------- #
def _error_codes(check: dict[str, Any]) -> list[str]:
    return [issue["code"] for issue in check.get("issues", ()) if issue.get("severity") == "error"]


# Expected-value problems the platform settles with the WinAMS capture instead
# of the model: the capture is the implementation's own output for the same
# inputs, recorded as EXECUTION_CAPTURED and re-executed before it counts.
# Only when they are the sole errors: a coverage gap or a diverged call path
# (EXECUTION_PATH_DIVERGED) means the inputs did not drive the intended paths,
# and capturing would overwrite that evidence with outputs of the wrong path.
_ORACLE_CODES = frozenset({"ORACLE_MISMATCH", "EXPECTED_VALUE_PENDING"})
# Human expected values may encode a requirement, so a mismatch there is a
# finding for the reviewer and is never overwritten; a capture is never
# captured again.
_CAPTURABLE_ORIGINS = frozenset({UtAttempt.ORIGIN_GENERATOR, UtAttempt.ORIGIN_LLM})


def _capture_backfill(job: UtJob, attempt: UtAttempt, check: dict[str, Any],
                      execution: Optional[dict[str, Any]]) -> bool:
    """Queue the WinAMS-captured CSV as the next attempt; True when queued."""
    if (execution or {}).get("evidence_source") == "HOST_EXECUTION" or check.get("evidence_source") == "HOST_EXECUTION":
        return False
    codes = set(_error_codes(check))
    if (check.get("verdict") != "FAIL" or attempt.origin not in _CAPTURABLE_ORIGINS
            or not codes or not codes <= _ORACLE_CODES):
        return False
    captured = Path((execution or {}).get("captured_candidate") or "")
    if not captured.name or not captured.is_file():
        return False
    csv_bytes = captured.read_bytes()
    if hashlib.sha256(csv_bytes).hexdigest() == attempt.csv_sha256:
        return False
    _new_attempt(job, UtAttempt.ORIGIN_CAPTURE, csv_bytes,
                 note=f"期望值由第 {attempt.seq} 版的 WinAMS 执行捕获回填（{', '.join(sorted(codes))}）",
                 reviser={"oracle_source": "EXECUTION_CAPTURED",
                          "from_attempt": attempt.seq,
                          "from_sha256": attempt.csv_sha256,
                          "captured_from": str(captured),
                          "resolved_codes": sorted(codes)})
    _gate(job, UtJob.EXECUTING)
    return True


_MISSING_CASE = "MISSING_CASE"


def _verified_obligation_ids(check: dict[str, Any], expected_sha: str = "") -> set[str]:
    witnesses = check.get("candidate_witnesses") or {}
    identified = (witnesses.get("contract") == "ut-agent.candidate-witness/1"
                  and (witnesses.get("evidence") or {}).get("identified") is True
                  and witnesses.get("candidate_sha256") == check.get("candidate_sha256")
                  and bool(check.get("candidate_sha256"))
                  and (not expected_sha or expected_sha == check["candidate_sha256"]))
    return set(witnesses.get("witnessed_obligation_ids") or ()) if identified else set()


def gap_status(check: dict[str, Any], revisers: list[dict[str, Any]],
               expected_sha: str = "") -> dict[str, Any]:
    """Missing cases of ``check`` against the claims and declines of model rounds.

    Only an explicit MISSING_CASE is work for the model; evidence gaps,
    unconfirmed infeasibility and reports without gap classes are not.
    """
    unresolved = (check.get("completeness") or {}).get("unresolved") or ()
    missing = [item for item in unresolved
               if item.get("gap_class") == _MISSING_CASE and item.get("obligation_id")]
    claimed: set[str] = set()
    declined: set[str] = set()
    for meta in revisers:
        claimed.update((meta or {}).get("claimed") or ())
        declined.update((meta or {}).get("declined") or ())
    ids = {item["obligation_id"] for item in missing}
    proven = _verified_obligation_ids(check, expected_sha)
    return {
        "missing": len(ids),
        "claimed": len(ids & claimed),
        "witnessed": len(ids & proven),
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


def next_state(check: dict[str, Any], origin: str, rounds: int, max_rounds: int,
               gaps: Optional[dict[str, Any]] = None) -> tuple[str, str, str]:
    """Pure transition after one checked attempt: (state, reason, gate code)."""
    verdict = check.get("verdict")
    codes = ", ".join(_error_codes(check))
    if verdict == "PASS":
        if gaps and gaps["open"] and origin != UtAttempt.ORIGIN_HUMAN:
            if rounds >= max_rounds:
                return (UtJob.NEEDS_HUMAN, f"仍有 {len(gaps['open'])} 个输入义务未证明，已到修订上限 {max_rounds}"
                        + _gap_note(gaps), GateCode.OPEN_GAPS_EXHAUSTED)
            return UtJob.REPAIRING, "", ""
        return UtJob.AWAITING_REVIEW, "机器校验通过，等待人工审核" + _gap_note(gaps), GateCode.MACHINE_PASS
    if verdict == "BLOCKED":
        blocked = next((issue for issue in check.get("issues", ())
                        if issue["code"] == "EXECUTION_BLOCKED"), None)
        if blocked is not None:
            reasons = blocked.get("detail") or []
            listed = "; ".join(
                f"{item.get('code')} {item.get('detail') or item.get('path', '')}"
                for item in reasons[:5])
            return UtJob.BLOCKED, f"WinAMS 执行环境不完整：{listed}", GateCode.ENV_BLOCKED
        return UtJob.NEEDS_HUMAN, f"存在无法自动修复的问题：{codes}", GateCode.CHECK_BLOCKED
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


def _previous_executable(job: UtJob, attempt: UtAttempt) -> Optional[UtAttempt]:
    parent_id = (attempt.reviser or {}).get("parent_attempt_id")
    if parent_id is not None:
        parent = db.session.get(UtAttempt, parent_id)
        if parent and parent.job_id == job.id and parent.seq < attempt.seq and parent.csv_path:
            return parent
        raise ServiceError("候选的父版本关系无效", code="INVALID_STATE")
    return (UtAttempt.query
            .filter(UtAttempt.job_id == job.id, UtAttempt.seq < attempt.seq,
                    UtAttempt.csv_path != "")
            .order_by(UtAttempt.seq.desc()).first())


def _fresh_exec_dir(job: UtJob, attempt: UtAttempt) -> Path:
    base = _job_dir(job) / f"a{attempt.seq:02d}-exec"
    candidate, run = base, 1
    while candidate.exists():
        run += 1
        candidate = base.with_name(f"{base.name}{run}")
    return candidate


def _execute_step(job: UtJob) -> None:
    attempt = db.session.get(UtAttempt, job.current_attempt_id)
    config = db.session.get(UtBatch, job.batch_id).config_snapshot or {}
    if config.get("execution_backend", "winams") == "clang":
        _execute_host_step(job, attempt, config)
        return
    candidate = Path(attempt.csv_path)
    previous = _previous_executable(job, attempt)
    intents = (job.generation or {}).get("intent_manifest")
    arguments = {
        "manifest": config["manifest_path"],
        "reference": Path(job.reference_csv) if job.reference_csv else None,
        "previous": Path(previous.csv_path) if previous else None,
    }
    if intents and Path(intents).is_file():
        arguments["intents"] = Path(intents)
    ir_json = (job.generation or {}).get("ir_json")
    if ir_json and Path(ir_json).is_file():
        arguments["ir_json"] = Path(ir_json)
    bridge = get_bridge()
    execution = None
    try:
        check = bridge.check(candidate, **arguments)
        # A candidate that fails the static check would only waste a WinAMS run.
        if check["verdict"] == "FORMAT_OK":
            out = _fresh_exec_dir(job, attempt)
            execute_args = {}
            if "intents" in arguments:
                execute_args["intents"] = arguments["intents"]
            with _WINAMS_GATE:
                execution = bridge.execute(
                    Path(config["work_root"]) / job.target.unit_rel, candidate, out,
                    stub_builds=_lines(config.get("stub_builds", "")),
                    source_rel=job.target.source_rel, **execute_args)
            check = bridge.check(candidate, execution=out / "execution.json", **arguments)
    except BridgeError as exc:
        _gate(job, UtJob.ERROR, f"ut-agent 调用失败：{exc}", GateCode.TOOL_ERROR)
        db.session.commit()
        return
    attempt.check = check
    attempt.execution = execution
    attempt.verdict = check["verdict"]
    job.last_verdict = check["verdict"]
    if _capture_backfill(job, attempt, check, execution):
        db.session.commit()
        return
    _gate(job, *next_state(check, attempt.origin, job.repair_rounds, job.max_repair_rounds,
                           _job_gaps(job, attempt)))
    db.session.commit()


def _execute_host_step(job: UtJob, attempt: UtAttempt, config: dict) -> None:
    """Host evidence is experimental and never enters the WinAMS checker/repair loop."""
    generation = job.generation or {}

    def persist(execution):
        attempt.execution = execution
        db.session.commit()

    def continuing():
        db.session.refresh(job, attribute_names=["stop_requested_at", "stop_requested_by"])
        return job.stop_requested_at is None

    try:
        execution, check = host.execute(
            bridge=get_bridge(), ir_json=generation.get("ir_json"),
            intents=generation.get("intent_manifest"), reference=job.reference_csv,
            candidate=attempt.csv_path, out=_fresh_exec_dir(job, attempt), config=config,
            model_timeout=current_app.config["UT_REPAIR_TIMEOUT"],
            persist=persist, continuing=continuing)
    except ProviderError as exc:
        _gate(job, UtJob.ERROR, f"本地模型不可用：{exc}", GateCode.MODEL_UNAVAILABLE)
    except BridgeError as exc:
        _gate(job, UtJob.ERROR, f"ut-agent 调用失败：{exc}", GateCode.TOOL_ERROR)
    else:
        attempt.execution, attempt.check, attempt.verdict = execution, check, check["verdict"]
        job.last_verdict = check["verdict"]
        if not continuing():
            _halt(job)
        elif check["verdict"] == "PASS":
            _gate(job, UtJob.AWAITING_REVIEW,
                  "Clang 主机候选校验通过，等待人工审核；不代表目标执行或正式交付验收", GateCode.MACHINE_PASS)
        else:
            reason = execution.get("reason") or "; ".join(
                str(issue.get("message", issue.get("code", ""))) for issue in check.get("issues", ()))
            _gate(job, UtJob.NEEDS_HUMAN, f"Clang 主机候选需人工处理：{reason}", GateCode.CHECK_BLOCKED)
    if not continuing():
        _halt(job)
    db.session.commit()


def _job_gaps(job: UtJob, attempt: UtAttempt) -> dict[str, Any]:
    """Gaps of `attempt` against the model rounds that shaped it.

    A staged proposal rejected before execution never became part of the
    lineage, so its claims and declines do not count; a round that added no
    rows (declines only) answered a lineage attempt and does.
    """
    attempts = {item.id: item for item in UtAttempt.query.filter_by(job_id=job.id)}
    lineage, cursor = set(), attempt.id
    while cursor in attempts and cursor not in lineage:
        lineage.add(cursor)
        cursor = (attempts[cursor].reviser or {}).get("parent_attempt_id")
    revisers = [item.reviser for item in attempts.values()
                if item.origin == UtAttempt.ORIGIN_LLM and (
                    item.id in lineage or (not item.csv_path and
                                           (item.reviser or {}).get("parent_attempt_id") in lineage))]
    return gap_status(attempt.check or {}, revisers, attempt.csv_sha256)


# Errors that appended input rows can address. Expected values belong to the
# capture, and format or column errors cannot be fixed by adding rows.
_ROW_FIXABLE = frozenset({"EXECUTION_PATH_DIVERGED", "ROW_COUNT_BELOW_REFERENCE",
                          "REFERENCE_CASE_REMOVED", "FORMAT_NO_DATA_ROWS"})


def _row_tags(check: dict[str, Any]) -> set[str]:
    """Error codes a model row may name besides a missing case (e.g. coverage)."""
    return {issue["code"] for issue in check.get("issues", ())
            if issue.get("severity") == "error" and issue.get("repairable")
            and (issue["code"].startswith("COVERAGE_") or issue["code"] in _ROW_FIXABLE)}


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
    return entry


def _repair_step(job: UtJob) -> None:
    attempt = db.session.get(UtAttempt, job.current_attempt_id)
    batch = db.session.get(UtBatch, job.batch_id)
    check = attempt.check or {}
    history = [_history_item(item) for item in
               UtAttempt.query.filter_by(job_id=job.id).order_by(UtAttempt.seq)]
    gaps = _job_gaps(job, attempt)
    generation = dict(job.generation or {})
    csv_bytes = Path(attempt.csv_path).read_bytes()
    # Freeze the cacheable prefix in durable job metadata, including restarts.
    # A changed prompt version starts a new snapshot; same-version drift is
    # rejected by revise rather than silently replacing semantic evidence.
    prefix = generation.get("repair_prompt_prefix")
    if not prefix or prefix.get("version") != repair.PROMPT_VERSION:
        try:
            prefix = repair.prompt_prefix(batch.constraints or {}, job.target.to_dict(), csv_bytes, check)
        except (UnicodeDecodeError, repair.increment.TableError):
            prefix = None
        if prefix is not None:
            generation["repair_prompt_prefix"] = prefix
            job.generation = generation
            db.session.commit()
    try:
        revision = repair.revise(
            constraints=batch.constraints or {},
            target=job.target.to_dict(),
            csv_bytes=csv_bytes,
            check=check,
            execution=attempt.execution,
            history=history,
            open_gaps=gaps["open"],
            allowed={item["obligation_id"] for item in gaps["open"]} | _row_tags(check),
            model=(batch.config_snapshot or {}).get("repair_model", ""),
            timeout=current_app.config["UT_REPAIR_TIMEOUT"],
            prefix_snapshot=prefix,
        )
    except ProviderError as exc:
        # The model never answered: no round is consumed.
        _gate(job, UtJob.ERROR, f"本地模型不可用：{exc}", GateCode.MODEL_UNAVAILABLE)
        db.session.commit()
        return
    if revision.meta.get("code") in {"PROMPT_PREFIX_CHANGED", "PROMPT_BUDGET_EXCEEDED"}:
        new = _new_attempt(job, UtAttempt.ORIGIN_LLM, None,
                           reviser={**revision.meta, "model_contacted": False},
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
            verification = get_bridge().check(Path(new.csv_path), **arguments)
        except BridgeError as exc:
            new.verdict = "INVALID"
            new.note = f"候选检查失败：{exc}"
            _gate(job, UtJob.ERROR, new.note, GateCode.TOOL_ERROR)
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
            job.state = UtJob.EXECUTING
        else:
            errors = ", ".join(_error_codes(verification))
            new.note = f"新增行在执行前被拒绝：{errors}；当前候选保持第 {attempt.seq} 版"
            if new.verdict == "BLOCKED" or job.repair_rounds >= job.max_repair_rounds:
                _gate(job, UtJob.NEEDS_HUMAN, new.note, GateCode.CANDIDATE_REJECTED)
        db.session.commit()
        return
    new.verdict = "INVALID"
    # No row was added: the checked attempt stays current. Declined gaps are
    # closed for the model, so a PASS goes to review once nothing is open.
    state, reason, code = next_state(check, attempt.origin, job.repair_rounds,
                                     job.max_repair_rounds, _job_gaps(job, attempt))
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
    jobs = (UtJob.query.filter(UtJob.state.in_(UtJob.ACTIVE))
            .with_for_update(of=UtJob).populate_existing().all())
    failed_batches: set[int] = set()
    for job in jobs:
        before_generation = job.state in (UtJob.QUEUED, UtJob.GENERATING)
        if job.id in pending_jobs or (before_generation and job.batch_id in pending_batches):
            result["pending"].append(job.id)
            continue
        if job.stop_requested_at is not None:
            _halt(job)
            result["stopped"].append(job.id)
        elif before_generation or job.current_attempt_id is None:
            _gate(job, UtJob.ERROR,
                  f"worker 在{_INTERRUPTED_STEP[job.state]}阶段中断，尚无可继续的候选；请从实施一览重新入队",
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
    job.state = UtJob.STOPPED
    job.reason = "已按请求停止；当前候选、机器判定与修订记录均已保留，可继续或重新入队"
    job.gate_code = GateCode.STOPPED


def _resume(job: UtJob) -> None:
    job.stop_requested_at = None
    job.stop_requested_by = None


def stop_job(project_id: int, job_id: int, user_id: int) -> UtJob:
    """Stop one active job.

    A queued job has no worker yet and stops at once.  A running one finishes
    its current external call (the WinAMS run or model reply is evidence worth
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
    if decision == "approve":
        if job.current_attempt_id is None:
            raise ServiceError("没有可通过的候选 CSV", code="INVALID_STATE")
        if job.state != UtJob.AWAITING_REVIEW and not note:
            raise ServiceError("机器校验未通过的候选需要填写审核意见才能通过", code="VALIDATION_ERROR")
        job.state = UtJob.APPROVED
    else:
        if not note:
            raise ServiceError("驳回必须填写原因", code="VALIDATION_ERROR")
        job.state = UtJob.REJECTED
    job.reviewed_by = user_id
    job.reviewed_at = _utcnow()
    job.review_note = note
    # The gate reason explained why the job waited; once decided it is stale.
    job.reason = ""
    job.gate_code = ""
    approved = job.state == UtJob.APPROVED
    audit.record("ut.job.approve" if approved else "ut.job.reject", actor_id=user_id,
                 object_type="ut_job", object_id=job.id, project_id=project_id,
                 new_value={"attempt_id": job.current_attempt_id,
                            "verdict": job.last_verdict or None, "note": note})
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
    wants_rows = attempt.verdict == "FAIL" or (
        attempt.verdict == "PASS" and _job_gaps(job, attempt)["open"])
    config = db.session.get(UtBatch, job.batch_id).config_snapshot or {}
    if config.get("execution_backend") == "clang" and config.get("stub_mode") == "llm":
        # A fresh model reply changes the reviewed candidate even when CSV bytes
        # stay identical or preparation failed before evidence was captured.
        # Preserve the previous attempt and stale-review fence in every case.
        _new_attempt(job, attempt.origin, Path(attempt.csv_path).read_bytes(),
                     note="Host stub retry; CSV unchanged, candidate evidence version renewed",
                     reviser={"host_retry_of": attempt.id}, user_id=user_id)
    _gate(job, UtJob.REPAIRING if wants_rows and config.get("execution_backend", "winams") != "clang"
          else UtJob.EXECUTING)
    db.session.commit()
    return job


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
    return [job.to_dict() for job in query.order_by(UtJob.id.desc()).limit(_JOB_LIST_LIMIT)]


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
    reached WinAMS; ``stalled`` proposed nothing at all, ``rejected`` only
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


def job_detail(project_id: int, job_id: int) -> dict[str, Any]:
    job = UtJob.query.filter_by(id=job_id, project_id=project_id).first()
    if job is None:
        raise ServiceError("作业不存在", code="NOT_FOUND")
    attempts = UtAttempt.query.filter_by(job_id=job.id).order_by(UtAttempt.seq).all()
    entry = job.to_dict(attempts=attempts)
    entry["batch"] = db.session.get(UtBatch, job.batch_id).to_dict()
    return entry


def attempt_csv(project_id: int, job_id: int, attempt_id: int) -> tuple[bytes, str]:
    attempt = (UtAttempt.query.join(UtJob, UtJob.id == UtAttempt.job_id)
               .filter(UtAttempt.id == attempt_id, UtJob.id == job_id,
                       UtJob.project_id == project_id).first())
    if attempt is None or not attempt.csv_path:
        raise ServiceError("该版本没有 CSV", code="NOT_FOUND")
    return Path(attempt.csv_path).read_bytes(), Path(attempt.csv_path).name


def attempt_artifact(project_id: int, job_id: int, attempt_id: int, artifact_id: str) -> tuple[bytes, str]:
    attempt = (UtAttempt.query.join(UtJob, UtJob.id == UtAttempt.job_id)
               .filter(UtAttempt.id == attempt_id, UtJob.id == job_id,
                       UtJob.project_id == project_id).first())
    if attempt is None:
        raise ServiceError("Artifact not found", code="NOT_FOUND")
    metadata = (attempt.execution or {}).get("stub_generation") or {}
    artifact = next((item for item in metadata.get("artifacts", ()) if item.get("id") == artifact_id), None)
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
