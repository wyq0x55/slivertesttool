"""Huey tasks for the unit-test pipeline (ut-agent + WinAMS + local LLM).

``ut_generate_batch`` runs one project-wide generation for a batch and then
enqueues ``ut_advance_job`` per ready job; each of those drives its job through
execute → check → repair until it reaches a human gate.
"""

from __future__ import annotations

import logging

from .huey_app import huey
from .tasks import _get_app

logger = logging.getLogger("silvetestapp.ut_tasks")


@huey.task()
def ut_generate_batch(batch_id: int) -> None:
    from ..services.ut_pipeline import pipeline

    with _get_app().app_context():
        ready = pipeline.run_generation(batch_id)
    for job_id in ready:
        ut_advance_job(job_id)


def recover_interrupted_jobs() -> dict:
    """Start-up recovery; the caller must run it before consumers start."""
    from ..services.ut_pipeline import pipeline

    pending = huey.pending(limit=10000)
    jobs = {task.args[0] for task in pending if isinstance(task, ut_advance_job.task_class)}
    batches = {task.args[0] for task in pending if isinstance(task, ut_generate_batch.task_class)}
    with _get_app().app_context():
        return pipeline.recover_interrupted(jobs, batches)


@huey.task()
def ut_advance_job(job_id: int) -> None:
    from ..extensions import db
    from ..models import UtJob
    from ..models.ut_pipeline import GateCode
    from ..services.ut_pipeline import pipeline

    with _get_app().app_context():
        try:
            pipeline.advance_job(job_id)
        except Exception as exc:  # noqa: BLE001 - never leave a job stuck in an active state
            logger.exception("ut job %s crashed", job_id)
            db.session.rollback()
            job = db.session.get(UtJob, job_id)
            if job is not None and job.state in UtJob.ACTIVE:
                job.state = UtJob.ERROR
                job.reason = f"内部错误：{type(exc).__name__}: {exc}"
                job.gate_code = GateCode.INTERNAL_ERROR
                db.session.commit()
