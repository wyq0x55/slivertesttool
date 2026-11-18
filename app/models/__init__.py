"""Database models."""

from __future__ import annotations

from .ai_draft import AiDraft, AiSignalDict
from .curve_view import CurveView
from .api_token import ApiToken
from .lanmatrix import (
    AuditLog,
    CellComment,
    CollabDoc,
    CollabPresence,
    CollabSyncMark,
    DashboardCaseFirstRun,
    DashboardCaseVersionLatest,
    DashboardDailyRollup,
    DashboardVersionRollup,
    DataJob,
    FieldDefinition,
    LMUser,
    Notification,
    Project,
    ProjectMember,
    ProjectModel,
    RowWriteback,
    SbsRevision,
    TestItemRow,
    TestRunRecord,
)
from .setting import Setting
from .run_evidence import RunEvidence
from .task import Task, TaskStatus
from .task_event import EventType, TaskEvent
from .ut_pipeline import UtAttempt, UtBatch, UtJob, UtPipelineConfig, UtTarget

__all__ = [
    "CurveView",
    "Setting",
    "Task",
    "TaskStatus",
    "TaskEvent",
    "EventType",
    "AiDraft",
    "ApiToken",
    "AiSignalDict",
    "RunEvidence",
    # LAN Test Matrix models (merged into the platform's model layer).
    "LMUser",
    "ProjectMember",
    "Project",
    "ProjectModel",
    "SbsRevision",
    "FieldDefinition",
    "TestItemRow",
    "TestRunRecord",
    "DashboardCaseFirstRun",
    "DashboardDailyRollup",
    "DashboardCaseVersionLatest",
    "DashboardVersionRollup",
    "RowWriteback",
    "Notification",
    "CellComment",
    "AuditLog",
    "DataJob",
    "CollabDoc",
    "CollabPresence",
    "CollabSyncMark",
    # Unit-test pipeline (ut-agent + Clang/LLVM execution + local LLM repair).
    "UtPipelineConfig",
    "UtTarget",
    "UtBatch",
    "UtJob",
    "UtAttempt",
]

import sys as _sys

if "app.services.lanmatrix.dashboard_rollup_service" not in _sys.modules:
    from ..services.lanmatrix.dashboard_rollup_service import register_rollup_listeners

    register_rollup_listeners()
