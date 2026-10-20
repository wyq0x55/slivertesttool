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

No machine outcome is final: every path ends at a human gate.

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
from ...models.lanmatrix import _utcnow
from ..ai.provider import ProviderError
from ..lanmatrix import audit
from ..lanmatrix import notification_service as notify_svc
from ..lanmatrix.errors import ServiceError, VersionConflict
from . import repair
from .bridge import BridgeError, BridgeSettings, UtAgentBridge

# The worker is one process with a thread pool.  WinAMS runs and project-wide
# extraction are serialised; checks and LLM revisions stay concurrent.
_WINAMS_GATE = threading.Lock()
_GENERATION_GATE = threading.Lock()
_EXPECTED_ATTEMPT_UNSET = object()

_CONFIG_TEXT_FIELDS = ("manifest_path", "index_csv", "product_root", "work_root",
                       "stub_builds", "repair_model")
_REQUIRED_FIELDS = ("manifest_path", "index_csv", "product_root", "work_root")
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
    if config is None or not all(getattr(config, key) for key in _REQUIRED_FIELDS):
        raise ServiceError(
            "请先配置 ut-agent 项目绑定（ProjectManifest / 索引 CSV / Product 根 / WinAMS 工作根）",
            code="UT_NOT_CONFIGURED")
    return config


def save_config(project_id: int, values: dict[str, Any], user_id: int) -> UtPipelineConfig:
    existing = get_config(project_id)
    before = existing.to_dict() if existing else None
    config = existing or UtPipelineConfig(
        project_id=project_id,
        max_repair_rounds=current_app.config["UT_REPAIR_MAX_ROUNDS"],
    )
    for key in _CONFIG_TEXT_FIELDS:
        if key in values:
            if not isinstance(values[key], str):
                raise ServiceError(f"{key} 必须是字符串", code="VALIDATION_ERROR")
            value = values[key].strip()
            if key == "stub_builds":
                value = "\n".join(_lines(value))
            setattr(config, key, value)
    if "max_repair_rounds" in values:
        rounds = values["max_repair_rounds"]
        if type(rounds) is not int or not 0 <= rounds <= 20:
            raise ServiceError("max_repair_rounds 必须是 0–20 的整数", code="VALIDATION_ERROR")
        config.max_repair_rounds = rounds
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
            "repair_rounds": job.repair_rounds, "last_verdict": job.last_verdict or None}


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


def _gate(job: UtJob, state: str, reason: str = "") -> None:
    reached_gate = state in UtJob.HUMAN_GATES and job.state != state
    job.state = state
    job.reason = reason
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
                 user_id: Optional[int] = None) -> UtAttempt:
    seq = (db.session.query(func.max(UtAttempt.seq)).filter_by(job_id=job.id).scalar() or 0) + 1
    path = digest = ""
    if csv_bytes is not None:
        directory = _job_dir(job)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"a{seq:02d}-{origin.lower()}.csv"
        target.write_bytes(csv_bytes)
        path, digest = str(target), hashlib.sha256(csv_bytes).hexdigest()
    attempt = UtAttempt(job_id=job.id, seq=seq, origin=origin, csv_path=path,
                        csv_sha256=digest, reviser=reviser, note=note, created_by=user_id)
    db.session.add(attempt)
    db.session.flush()
    if path:
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
            _gate(job, UtJob.ERROR, f"生成失败：{exc}")
        db.session.commit()
        return []
    batch.constraints = constraints
    batch.provenance = document.get("provenance")
    units = {unit["function"]: unit for unit in document.get("units", ())}
    ready = []
    for job in jobs:
        unit = units.get(job.target.function)
        if unit is None:
            _gate(job, UtJob.ERROR, "生成结果中缺少该函数")
            continue
        job.generation_status = str(unit.get("status") or "")
        job.generation = unit
        if not unit.get("testcsv"):
            detail = unit.get("error") or unit.get("projection_failure") or ""
            _gate(job, UtJob.NEEDS_HUMAN,
                  f"生成器未产出 TestCsv（{job.generation_status}）：{detail}")
            continue
        attempt = _new_attempt(job, UtAttempt.ORIGIN_GENERATOR,
                               Path(unit["testcsv"]).read_bytes(),
                               note=f"generation status {job.generation_status}")
        # The generator CSV is the baseline reference every revision must keep.
        job.reference_csv = attempt.csv_path
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


def gap_status(check: dict[str, Any], revisers: list[dict[str, Any]]) -> dict[str, Any]:
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
    return {
        "missing": len(ids),
        "claimed": len(ids & claimed),
        "declined": len((ids & declined) - claimed),
        "open": [item for item in missing
                 if item["obligation_id"] not in claimed and item["obligation_id"] not in declined],
    }


def _gap_note(gaps: Optional[dict[str, Any]]) -> str:
    if not gaps or not gaps["missing"]:
        return ""
    return (f"；缺用例 {gaps['missing']} 个：模型补行待确认 {gaps['claimed']}，"
            f"模型声明无法补齐 {gaps['declined']}，未处理 {len(gaps['open'])}")


def next_state(check: dict[str, Any], origin: str, rounds: int, max_rounds: int,
               gaps: Optional[dict[str, Any]] = None) -> tuple[str, str]:
    """Pure transition after one checked attempt."""
    verdict = check.get("verdict")
    codes = ", ".join(_error_codes(check))
    if verdict == "PASS":
        if gaps and gaps["open"] and origin != UtAttempt.ORIGIN_HUMAN and rounds < max_rounds:
            return UtJob.REPAIRING, ""
        return UtJob.AWAITING_REVIEW, "机器校验通过，等待人工审核" + _gap_note(gaps)
    if verdict == "BLOCKED":
        blocked = next((issue for issue in check.get("issues", ())
                        if issue["code"] == "EXECUTION_BLOCKED"), None)
        if blocked is not None:
            reasons = blocked.get("detail") or []
            listed = "; ".join(
                f"{item.get('code')} {item.get('detail') or item.get('path', '')}"
                for item in reasons[:5])
            return UtJob.BLOCKED, f"WinAMS 执行环境不完整：{listed}"
        return UtJob.NEEDS_HUMAN, f"存在无法自动修复的问题：{codes}"
    if verdict != "FAIL":
        return UtJob.NEEDS_HUMAN, f"未知判定：{verdict}"
    if origin == UtAttempt.ORIGIN_HUMAN:
        return UtJob.NEEDS_HUMAN, f"人工修订仍未通过校验：{codes}"
    if not _row_tags(check) and not (gaps and gaps["open"]):
        # The model only adds input rows; nothing here can be closed that way.
        return UtJob.NEEDS_HUMAN, f"剩余问题无法通过新增输入行解决：{codes}"
    if rounds >= max_rounds:
        return UtJob.NEEDS_HUMAN, f"本地模型已修正 {rounds} 次仍未通过（上限 {max_rounds}）：{codes}"
    return UtJob.REPAIRING, ""


def _previous_executable(job: UtJob, attempt: UtAttempt) -> Optional[UtAttempt]:
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
    bridge = get_bridge()
    execution = None
    try:
        check = bridge.check(candidate, **arguments)
        # A candidate that fails the static check would only waste a WinAMS run.
        if check["verdict"] == "FORMAT_OK":
            out = _fresh_exec_dir(job, attempt)
            with _WINAMS_GATE:
                execution = bridge.execute(
                    Path(config["work_root"]) / job.target.unit_rel, candidate, out,
                    stub_builds=_lines(config.get("stub_builds", "")),
                    source_rel=job.target.source_rel)
            check = bridge.check(candidate, execution=out / "execution.json", **arguments)
    except BridgeError as exc:
        _gate(job, UtJob.ERROR, f"ut-agent 调用失败：{exc}")
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
                           _job_gaps(job, check)))
    db.session.commit()


def _job_gaps(job: UtJob, check: dict[str, Any]) -> dict[str, Any]:
    revisers = [item.reviser for item in UtAttempt.query.filter_by(
        job_id=job.id, origin=UtAttempt.ORIGIN_LLM)]
    return gap_status(check, revisers)


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
    return entry


def _repair_step(job: UtJob) -> None:
    attempt = db.session.get(UtAttempt, job.current_attempt_id)
    batch = db.session.get(UtBatch, job.batch_id)
    check = attempt.check or {}
    history = [_history_item(item) for item in
               UtAttempt.query.filter_by(job_id=job.id).order_by(UtAttempt.seq)]
    gaps = _job_gaps(job, check)
    try:
        revision = repair.revise(
            constraints=batch.constraints or {},
            target=job.target.to_dict(),
            csv_bytes=Path(attempt.csv_path).read_bytes(),
            check=check,
            execution=attempt.execution,
            history=history,
            open_gaps=gaps["open"],
            allowed={item["obligation_id"] for item in gaps["open"]} | _row_tags(check),
            model=(batch.config_snapshot or {}).get("repair_model", ""),
            timeout=current_app.config["UT_REPAIR_TIMEOUT"],
        )
    except ProviderError as exc:
        # The model never answered: no round is consumed.
        _gate(job, UtJob.ERROR, f"本地模型不可用：{exc}")
        db.session.commit()
        return
    job.repair_rounds += 1
    new = _new_attempt(job, UtAttempt.ORIGIN_LLM, revision.csv_bytes,
                       reviser=revision.meta, note=revision.meta.get("error", ""))
    if revision.csv_bytes is not None:
        job.state = UtJob.EXECUTING
        db.session.commit()
        return
    new.verdict = "INVALID"
    # No row was added: the checked attempt stays current. Declined gaps are
    # closed for the model, so a PASS goes to review once nothing is open.
    state, reason = next_state(check, attempt.origin, job.repair_rounds,
                               job.max_repair_rounds, _job_gaps(job, check))
    if (state == UtJob.NEEDS_HUMAN and check.get("verdict") == "FAIL"
            and job.repair_rounds >= job.max_repair_rounds):
        reason = (f"本地模型已修正 {job.repair_rounds} 次仍未产出可用候选"
                  f"（上限 {job.max_repair_rounds}）")
    elif state == UtJob.NEEDS_HUMAN and revision.meta.get("error"):
        reason = f"{revision.meta['error']}；{reason}"
    if state != UtJob.REPAIRING:
        _gate(job, state, reason)
    db.session.commit()


def advance_job(job_id: int) -> None:
    """Drive one job until it reaches a human gate."""
    batch_id = None
    while True:
        job = db.session.get(UtJob, job_id)
        if job is None:
            return
        batch_id = job.batch_id
        if job.state == UtJob.EXECUTING:
            _execute_step(job)
        elif job.state == UtJob.REPAIRING:
            _repair_step(job)
        else:
            break
    _refresh_batch(batch_id)


# --------------------------------------------------------------------------- #
# Human actions
# --------------------------------------------------------------------------- #
def _locked_job(project_id: int, job_id: int) -> UtJob:
    job = (UtJob.query.filter_by(id=job_id, project_id=project_id)
           .with_for_update(of=UtJob).populate_existing().first())
    if job is None:
        raise ServiceError("作业不存在", code="NOT_FOUND")
    if job.state not in UtJob.HUMAN_GATES:
        raise ServiceError(f"作业当前状态为 {job.state}，不接受该操作", code="INVALID_STATE")
    return job


def submit_revision(project_id: int, job_id: int, csv_bytes: bytes, user_id: int,
                    note: str = "") -> UtJob:
    if not csv_bytes:
        raise ServiceError("CSV 不能为空", code="VALIDATION_ERROR")
    job = _locked_job(project_id, job_id)
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
    if job.state not in (UtJob.BLOCKED, UtJob.ERROR):
        raise ServiceError("只有 blocked / error 状态的作业可以重试", code="INVALID_STATE")
    attempt = db.session.get(UtAttempt, job.current_attempt_id) if job.current_attempt_id else None
    if attempt is None:
        raise ServiceError("生成阶段失败的作业请重新加入队列", code="INVALID_STATE")
    audit.record("ut.job.retry", actor_id=user_id, object_type="ut_job", object_id=job.id,
                 project_id=project_id, old_value={"state": job.state, "reason": job.reason})
    # An error during repair leaves a checked attempt that still wants rows
    # (FAIL, or PASS with open missing cases): resume the repair.
    wants_rows = attempt.verdict == "FAIL" or (
        attempt.verdict == "PASS" and _job_gaps(job, attempt.check or {})["open"])
    _gate(job, UtJob.REPAIRING if wants_rows else UtJob.EXECUTING)
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
