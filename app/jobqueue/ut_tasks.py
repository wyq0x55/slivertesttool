"""Huey tasks for the unit-test pipeline (ut-agent + WinAMS + local LLM).

``ut_generate_batch`` runs one project-wide generation for a batch and
enqueues ``ut_advance_job`` as each function is finalized; each drives its job through
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
        try:
            pipeline.run_generation(batch_id, on_ready=ut_advance_job)
        except pipeline.GenerationBusy:
            ut_generate_batch.schedule(args=(batch_id,), delay=2)


def recover_interrupted_jobs() -> dict:
    """Start-up recovery; the caller must run it before consumers start."""
    from ..services.ut_pipeline import pipeline

    pending = [*huey.pending(limit=10000), *huey.scheduled(limit=10000)]
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
                from ..services.ut_pipeline import timing

                timing.interrupt(job)
                pipeline._set_state(job, UtJob.ERROR)
                job.reason = f"内部错误：{type(exc).__name__}: {exc}"
                job.gate_code = GateCode.INTERNAL_ERROR
                db.session.commit()
