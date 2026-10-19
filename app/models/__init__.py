"""Database models."""

from __future__ import annotations

from .ai_draft import AiDraft, AiSignalDict
from .lanmatrix import (
    AuditLog,
    CellComment,
    CollabDoc,
    CollabPresence,
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
    "Setting",
    "Task",
    "TaskStatus",
    "TaskEvent",
    "EventType",
    "AiDraft",
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
    "RowWriteback",
    "Notification",
    "CellComment",
    "AuditLog",
    "DataJob",
    "CollabDoc",
    "CollabPresence",
    # Unit-test pipeline (ut-agent + WinAMS + local LLM repair).
    "UtPipelineConfig",
    "UtTarget",
    "UtBatch",
    "UtJob",
    "UtAttempt",
]
