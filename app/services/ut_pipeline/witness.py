"""Run ut-agent's witness triage and proposal for a job, on operator request.

Both steps read the generation inputs silver already stores and write only to a
new directory under the batch.  They do not change the job's state, do not
write the manifest or the witness ledger, and never produce expected values:
the output is a candidate list a person still has to confirm.

The model is called only from :func:`propose`, which is reachable only from an
explicit operator action; nothing in the batch lifecycle calls it.
"""

from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from ...extensions import db
from ...models import UtBatch, UtJob
from ..ai import config as ai_config
from ..lanmatrix import audit
from ..lanmatrix.errors import ServiceError
from . import handoff
from .bridge import BridgeError, UtAgentBridge

_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
RUNNING = "RUNNING"


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _job(project_id: int, job_id: int) -> UtJob:
    job = UtJob.query.filter_by(id=job_id, project_id=project_id).one_or_none()
    if job is None:
        raise ServiceError("作业不存在", code="NOT_FOUND")
    return job


def _inputs(job: UtJob) -> tuple[str, str]:
    """The manifest and the two generation artifacts a witness run reads."""
    if job.superseded_by:
        raise ServiceError(f"该作业已被作业 {job.superseded_by} 取代，只能在最新作业上分拣",
                           code="SUPERSEDED")
    if not handoff.ready(job):
        state = handoff.state(job)
        if state == handoff.NO_SUITE:
            raise ServiceError("该函数没有可用于分拣的 suite（生成在义务循环之前耗尽或进程超时），"
                               "缺少分拣输入", code="VALIDATION_ERROR")
        if state == handoff.NO_ADMISSIBLE_OBLIGATION:
            raise ServiceError("该函数的未完成义务没有可提议的项，无需分拣",
                               code="VALIDATION_ERROR")
        raise ServiceError("分拣记录不可用（" + str(handoff.handoff(job).get("reason") or "未知原因")
                           + "），无法分拣", code="VALIDATION_ERROR")
    batch = UtBatch.query.filter_by(id=job.batch_id).one_or_none()
    config = (batch.config_snapshot or {}) if batch is not None else {}
    manifest = str(config.get("manifest_path") or "")
    # A recorded server path is checked here; a relative one is resolved by the
    # tool itself, so only its own failure can report on it.
    if not manifest or (Path(manifest).is_absolute() and not Path(manifest).is_file()):
        raise ServiceError("该批次没有可读取的项目清单文件，无法分拣",
                           code="VALIDATION_ERROR")
    generation = job.generation or {}
    ir = str(generation.get("ir_json") or "")
    suite = str(generation.get("intent_manifest") or "")
    if not Path(ir).is_file() or not Path(suite).is_file():
        raise ServiceError("该作业缺少生成产物（FunctionIR 或 test-intents），无法分拣",
                           code="VALIDATION_ERROR")
    return manifest, ir, suite


def _bridge(bridge: UtAgentBridge | None) -> UtAgentBridge:
    if bridge is not None:
        return bridge
    from .pipeline import get_bridge

    return get_bridge()


def _witness_root(job: UtJob) -> Path:
    from .pipeline import _batch_dir

    return _batch_dir(job.project_id, job.batch_id) / "witness" / f"job{job.id}"


def _next_run(job: UtJob) -> int:
    """The next unused run number, so an earlier run's receipts are never reused."""
    root = _witness_root(job)
    used = [0]
    for entry in root.glob("run*") if root.is_dir() else ():
        try:
            used.append(int(entry.name[3:]))
        except ValueError:
            continue
    return max(used) + 1


def _store(job: UtJob, section: str, value: dict) -> dict:
    document = copy.deepcopy(handoff.document(job))
    document["contract"] = handoff.CONTRACT
    document[section] = value
    job.witness = document
    return document


def _running(document: dict, section: str) -> bool:
    current = document.get(section)
    return isinstance(current, dict) and current.get("status") == RUNNING


def _reject_running(job: UtJob, section: str, label: str) -> None:
    if _running(handoff.document(job), section):
        raise ServiceError(f"该作业的{label}正在进行，请等待结果",
                           code="CONFLICT")


def _call(label: str, action, *, on_failure=None) -> dict | None:
    """Run one witness command; a failure is recorded instead of stranding the run.

    The caller already returned to the operator, so a failure has to land in the
    job's witness record, and ``None`` tells the caller nothing is left to read.
    Leaving the section at ``RUNNING`` would read as work still in progress.
    """
    try:
        return action()
    except BridgeError as exc:
        failure = ServiceError(f"{label}失败：{exc}", code="BRIDGE_ERROR")
        if on_failure is None:
            raise failure from exc
        on_failure(failure)
        return None


def triage(project_id: int, job_id: int, *, actor_id: int | None = None,
           batch_size: int = 8, bridge: UtAgentBridge | None = None) -> dict:
    """Sort the job's unsolved obligations; no model, no ledger, no manifest write."""
    job = _job(project_id, job_id)
    _reject_running(job, "triage", "分拣")
    manifest, ir, suite = _inputs(job)
    run = _next_run(job)
    out = _witness_root(job) / f"run{run}" / "triage"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        raise ServiceError("分拣目录已存在，拒绝覆盖既有证据", code="CONFLICT")
    _store(job, "triage", {"status": RUNNING, "out": str(out), "run": run,
                           "started_at": _iso_now(), "actor_id": actor_id})
    db.session.commit()
    result = _call("分拣", lambda: _bridge(bridge).triage(
        manifest=manifest, ir=ir, suite=suite, out=out, batch_size=batch_size),
        on_failure=lambda failure: _store(job, "triage", {
            "status": "FAILED", "error": str(failure), "out": str(out), "run": run,
            "finished_at": _iso_now(), "actor_id": actor_id}))
    if result is None:
        db.session.commit()
        return handoff.document(job)["triage"]
    document = {
        "status": str(result.get("status") or ""),
        "proposable": result.get("proposable"),
        "excluded": result.get("excluded"),
        "excluded_by_cause": result.get("excluded_by_cause") or {},
        "out": str(out),
        "run": run,
        "triage_sha256": _sha256(out / "triage.json"),
        "finished_at": _iso_now(),
        "actor_id": actor_id,
    }
    _store(job, "triage", document)
    audit.record("ut.witness.triage", actor_id=actor_id, object_type="ut_job",
                 object_id=job.id, project_id=job.project_id,
                 new_value={"run": run, "status": document["status"]})
    db.session.commit()
    return document


def clear_running() -> int:
    """Park witness runs whose worker died, so a restart does not leave them RUNNING.

    Called at worker start-up with the other recovery work: the sections are set
    to RUNNING only inside the task, so a RUNNING section at start-up has no
    live owner and can never finish on its own.
    """
    cleared = 0
    for job in UtJob.query.filter(UtJob.witness.isnot(None)).all():
        document = handoff.document(job)
        changed = False
        for section, label in (("triage", "分拣"), ("proposal", "提议"), ("confirm", "确认")):
            if _running(document, section):
                document = _store(job, section, {
                    "status": "FAILED", "error": f"worker 在{label}阶段中断，请重新发起",
                    "finished_at": _iso_now()})
                changed = True
        if changed:
            cleared += 1
    if cleared:
        db.session.commit()
    return cleared


def _loopback(base_url: str) -> None:
    """Refuse a non-loopback base URL before any work starts, as ut-agent does."""
    parts = urlsplit(base_url)
    if (parts.scheme != "http" or parts.hostname not in _LOOPBACK
            or parts.username or parts.password or parts.query or parts.fragment):
        raise ServiceError("本地模型地址必须是 loopback HTTP 地址，例如 http://127.0.0.1:3425/v1",
                           code="VALIDATION_ERROR")


def _require_triage(job: UtJob) -> dict:
    """The finished triage a proposal continues from."""
    section = handoff.document(job).get("triage") or {}
    if section.get("status") != "PROPOSABLE":
        raise ServiceError("该作业还没有可提议的分拣结果（需要先完成分拣且状态为 PROPOSABLE）",
                           code="VALIDATION_ERROR")
    return section


def _model() -> tuple[str, str]:
    """The configured local model, refusing a non-loopback base URL up front."""
    settings = ai_config.get_ai_config(include_secret=True)
    base_url = str(settings.get(ai_config.KEY_API_BASE) or "")
    model = str(settings.get(ai_config.KEY_MODEL) or "")
    if not base_url or not model or not settings.get(ai_config.KEY_API_KEY):
        raise ServiceError("本地模型未配置完整（需要 base URL、模型名与 API key）",
                           code="VALIDATION_ERROR")
    _loopback(base_url)
    return base_url, model


def check(project_id: int, job_id: int, step: str) -> None:
    """Reject a request the worker would only fail, before it is queued."""
    job = _job(project_id, job_id)
    if step == "triage":
        _reject_running(job, "triage", "分拣")
        _inputs(job)
        return
    _reject_running(job, "proposal", "提议")
    _require_triage(job)
    _inputs(job)
    _model()


def propose(project_id: int, job_id: int, *, actor_id: int | None = None,
            batch_size: int = 8, bridge: UtAgentBridge | None = None) -> dict:
    """Ask the configured local model for candidates from a finished triage.

    Reached only from an operator action: the platform never schedules it.
    """
    job = _job(project_id, job_id)
    _reject_running(job, "proposal", "提议")
    triage_result = _require_triage(job)
    manifest, ir, suite = _inputs(job)
    base_url, model = _model()
    run = int(triage_result.get("run") or 0)
    out = _witness_root(job) / f"run{run}" / "propose"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        raise ServiceError("提议目录已存在，拒绝覆盖既有证据", code="CONFLICT")
    _store(job, "proposal", {"status": RUNNING, "out": str(out), "run": run,
                             "source": "PROPOSE", "model": model,
                             "started_at": _iso_now(), "actor_id": actor_id})
    db.session.commit()
    result = _call("提议", lambda: _bridge(bridge).propose(
        manifest=manifest, ir=ir, suite=suite, out=out, base_url=base_url,
        model=model, batch_size=batch_size),
        on_failure=lambda failure: _store(job, "proposal", {
            "status": "FAILED", "source": "PROPOSE", "error": str(failure),
            "out": str(out), "run": run, "model": model,
            "finished_at": _iso_now(), "actor_id": actor_id}))
    if result is None:
        db.session.commit()
        return handoff.document(job)["proposal"]
    document = {
        "status": str(result.get("status") or ""),
        "source": "PROPOSE",
        "proposable": result.get("proposable"),
        "excluded": result.get("excluded"),
        "out": str(out),
        "run": run,
        "model": model,
        "candidates": str(out / "candidates.json"),
        "candidates_sha256": _sha256(out / "candidates.json"),
        "finished_at": _iso_now(),
        "actor_id": actor_id,
    }
    _store(job, "proposal", document)
    audit.record("ut.witness.propose", actor_id=actor_id, object_type="ut_job",
                 object_id=job.id, project_id=job.project_id,
                 new_value={"run": run, "status": document["status"], "model": model})
    db.session.commit()
    return document