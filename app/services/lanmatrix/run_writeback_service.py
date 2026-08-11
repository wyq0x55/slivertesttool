"""Mirror a finished run onto the Test-Matrix row(s) it was executed for.

The editor's value is that the spreadsheet *is* the report: a reviewer opens the
matrix and sees, per case, not only the verdict but who produced it, against
which model version, and when. Previously only the verdict (``result`` / 結果)
flowed back, so every other evidence column had to be typed in by hand -- the
one thing a person is guaranteed to eventually get wrong.

This module writes the full evidence set in one place:

===============  ==================  =====================================
field key        Excel column        source
===============  ==================  =====================================
``result``       結果                the task verdict
``version_label`` バージョン         ``ProjectModel.version`` (else its name)
``executor``     実施者              submitter's display name
``exec_date``    実施日              ``finished_at`` in ``LM_DISPLAY_TZ``
``log``          ログ                the ``task_key`` that produced it
===============  ==================  =====================================

Every finished run additionally appends an immutable
:class:`~app.models.lanmatrix.TestRunRecord`. The row columns can only ever show
the *latest* run; the dashboard's progress, burn-up and per-version comparisons
all need the ones before it.

Writes go through :func:`app.collab.writeback.apply_server_fields` rather than
touching the row directly, so a project that is open in the collaborative editor
does not silently revert the verdict on its next flush.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from flask import current_app

from ...extensions import db
from ...models import (LMUser, Project, ProjectModel, Task, TestItemRow,
                       TestRunRecord)
from ...collab import writeback
from . import review_service, silver_json_export as sje

logger = logging.getLogger(__name__)

# Verdict string -> normalised bucket. Everything the dashboard aggregates uses
# these buckets, so verdict parsing lives here once instead of in every chart.
_OUTCOMES = {
    "pass": "pass", "passed": "pass", "ok": "pass", "success": "pass",
    "fail": "fail", "failed": "fail", "ng": "fail",
    "error": "error", "exception": "error",
    "cancelled": "cancelled", "canceled": "cancelled",
    "untestable": "untestable",
}

#: Verdict reserved for a case a human has judged impossible to test. It is
#: never produced by a runner -- only set manually -- but it is recognised here
#: so the dashboard and the review flow agree on one vocabulary.
UNTESTABLE = "Untestable"


def classify(verdict: str) -> str:
    """Normalise a raw verdict into a dashboard bucket ("" when unknown)."""
    return _OUTCOMES.get((verdict or "").strip().lower(), "")


def _display_timezone():
    """Resolve ``LM_DISPLAY_TZ`` into a tzinfo, falling back to UTC.

    A misconfigured timezone must not take a run down with it, so an unknown
    name is logged and degraded rather than raised.
    """
    name = (current_app.config.get("LM_DISPLAY_TZ") or "").strip()
    if not name:
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        logger.warning("unknown LM_DISPLAY_TZ %r, falling back to UTC", name)
        return timezone.utc


def local_date(moment: datetime | None) -> str:
    """Format a naive-UTC timestamp as a ``YYYY-MM-DD`` local calendar date."""
    if moment is None:
        return ""
    aware = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    return aware.astimezone(_display_timezone()).strftime("%Y-%m-%d")


def _executor_name(task: Task) -> str:
    """Best available human label for whoever submitted the run.

    Preference order is display name -> username -> the legacy free-text
    ``submitter`` column, because 実施者 is read by people, not by code.
    """
    if task.submitter_id:
        user = db.session.get(LMUser, task.submitter_id)
        if user is not None:
            return (user.display_name or user.username or "").strip()
    return (task.submitter or "").strip()


def _model_identity(task: Task) -> tuple[str, str]:
    """Return ``(model_name, model_version)`` for the model this run used.

    Matching is by the model *name* recorded on the task (``sil_name``): the
    task does not hold a foreign key to the registry, and back-filling one would
    rewrite history whenever a model is re-registered. When no registry entry
    matches -- a legacy in-bundle run -- the version is reported as empty rather
    than guessed, so the dashboard can show "unversioned" honestly.
    """
    name = (task.sil_name or "").strip()
    if not name or not task.project_id:
        return name, ""
    model = (ProjectModel.query
             .filter_by(project_id=task.project_id, name=name)
             .order_by(ProjectModel.id.desc())
             .first())
    if model is None:
        return name, ""
    return model.name or name, (model.version or "").strip()


def build_row_values(task: Task, verdict: str) -> dict[str, Any]:
    """The ``field_key -> value`` evidence set for one finished run."""
    _model_name, version = _model_identity(task)
    values: dict[str, Any] = {
        "result": (verdict or "")[:24],
        "executor": _executor_name(task),
        "exec_date": local_date(task.finished_at),
    }
    # Only stamp a version when we actually know one: writing "" would erase a
    # label a human had filled in by hand.
    if version:
        values["version_label"] = version
    if current_app.config.get("LM_WRITEBACK_LOG_COLUMN", True) and task.task_key:
        values["log"] = task.task_key
    return values


def _matching_rows(project_id: int, test_id: str) -> list[TestItemRow]:
    """Rows of ``project_id`` whose logical test id is ``test_id``.

    The logical id is the row's ``test_id`` field when set, else its
    ``case_id``. The obvious query is a full scan plus a Python-side comparison;
    instead we let the database narrow the candidates down first, which keeps a
    project with tens of thousands of rows from doing a full table read on every
    finished run.

    The narrowing must go through the *portable* JSON comparator,
    ``.as_string()``. It is tempting to reach for the JSONB accessor
    ``.astext`` because ``custom_values`` is declared
    ``JSON().with_variant(JSONB, "postgresql")`` -- but ``with_variant`` only
    swaps the type when the statement is compiled for a dialect. The
    Python-side comparator is always built from the *base* type (``JSON``), so
    ``.astext`` raises ``AttributeError`` on every dialect, PostgreSQL
    included. Because the caller treats write-back as best-effort, that
    exception was logged and swallowed, silently dropping the evidence instead
    of failing loudly.

    ``.as_string()`` compiles to ``->>`` on PostgreSQL and ``JSON_EXTRACT`` on
    SQLite, so this stays an exact, index-friendly predicate rather than a
    full-table scan. Correctness is still confirmed by re-checking
    :func:`row_test_id` in Python below, which also settles precedence when a
    row has both a ``case_id`` and a differing ``test_id`` field.
    """
    needle = (test_id or "").strip()
    if not needle:
        return []
    candidates = (TestItemRow.query
                  .filter_by(project_id=project_id, sheet="test", deleted_at=None)
                  .filter(db.or_(
                      TestItemRow.case_id == needle,
                      TestItemRow.custom_values["test_id"].as_string() == needle))
                  .all())
    return [row for row in candidates if sje.row_test_id(row) == needle]


def record_run(task: Task, verdict: str) -> int:
    """Write the run's evidence onto its row(s) and append a run record.

    Returns the number of rows updated, ``0`` when there was legitimately
    nothing to write, and ``-1`` when the write-back FAILED. The three used to
    be indistinguishable: a swallowed exception looked exactly like "no matrix
    row matches this test id", which is how a broken loop stayed invisible.
    Still best-effort -- a write-back problem must never fail a run that already
    completed -- but now it is loud.
    """
    if not task.project_id or not task.test_id:
        return 0
    try:
        rows = _matching_rows(task.project_id, task.test_id)
        if not rows:
            logger.info("no matrix row matches test_id=%r (project=%s, task=%s)",
                        task.test_id, task.project_id, task.task_key)
            return 0

        values = build_row_values(task, verdict)
        model_name, model_version = _model_identity(task)
        executed_at = task.finished_at or datetime.now(timezone.utc).replace(tzinfo=None)
        executed_on = values["exec_date"]
        executor_name = values["executor"]
        outcome = classify(verdict)

        project = db.session.get(Project, task.project_id)

        changed = 0
        for row in rows:
            if writeback.apply_server_fields(task.project_id, row, values):
                changed += 1
            # A verdict is a claim; the project's policy decides whether it needs
            # a second pair of eyes before it counts as accepted evidence.
            if project is not None:
                review_service.request_review(project, row, verdict,
                                              actor_id=task.submitter_id)
            # The record is appended even when no cell value changed: re-running
            # a case that passes again is still a run, and the burn-up chart
            # would otherwise show a flat line during a regression sweep.
            db.session.add(TestRunRecord(
                project_id=task.project_id,
                row_uuid=row.uuid,
                test_id=task.test_id,
                task_key=task.task_key or "",
                verdict=(verdict or "")[:24],
                outcome=outcome,
                model_name=model_name,
                model_version=model_version,
                executor_id=task.submitter_id,
                executor_name=executor_name,
                executed_at=executed_at,
                executed_on=executed_on,
            ))
        db.session.commit()
        return changed
    except Exception:  # pragma: no cover - defensive, never break the run
        try:
            db.session.rollback()
        except Exception:
            logger.exception("rollback after failed run write-back also failed")
        logger.warning(
            "run write-back FAILED: task=%s test_id=%r project=%s -- the run "
            "finished but its evidence columns were not written",
            task.task_key, task.test_id, task.project_id)
        logger.exception("failed to write run evidence back onto TestItemRow")
        return -1
