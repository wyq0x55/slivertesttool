"""Canonical typed boundary for owner-selected efficiency pilot audits."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


def _nonblank(value: str) -> str:
    if not value.strip() or any(ord(character) < 32 for character in value):
        raise ValueError("A nonblank reference without control characters is required")
    return value


PositiveId = Annotated[int, Field(strict=True, gt=0)]
Reference = Annotated[str, Field(strict=True, min_length=1, max_length=256), AfterValidator(_nonblank)]
ModuleName = Annotated[str, Field(strict=True, min_length=1, max_length=128), AfterValidator(_nonblank)]
Digest = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
PositiveMinutes = Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]
NonnegativeMinutes = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]


class ContractModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)


class PilotModel(ContractModel):
    model_id: PositiveId
    version: Annotated[str, Field(strict=True, max_length=64)]
    sha256: Digest


class PilotViewpoint(ContractModel):
    item_id: PositiveId
    version: PositiveId
    module: ModuleName
    document_revision: Reference
    approval_reference: Reference


class PilotAttempt(ContractModel):
    task_key: Annotated[str, Field(strict=True, pattern=r"^T[0-9]{1,15}$")]
    run_count: PositiveId
    item_id: PositiveId


class PilotTimings(ContractModel):
    manual_baseline_minutes: PositiveMinutes | None = None
    assisted_total_minutes: PositiveMinutes | None = None
    human_intervention_minutes: NonnegativeMinutes | None = None
    human_intervention_count: Annotated[int, Field(strict=True, ge=0)] | None = None


class PilotInput(ContractModel):
    schema_version: Annotated[int, Field(strict=True, ge=1, le=1)]
    project_id: PositiveId
    modules: Annotated[list[ModuleName], Field(min_length=2, max_length=2)]
    model: PilotModel
    viewpoints: Annotated[list[PilotViewpoint], Field(min_length=20, max_length=20)]
    draft_ids: Annotated[list[PositiveId], Field(max_length=100)] = Field(default_factory=list)
    run_attempts: Annotated[list[PilotAttempt], Field(max_length=100)] = Field(default_factory=list)
    timings: PilotTimings = Field(default_factory=PilotTimings)

    @model_validator(mode="after")
    def validate_selection(self):
        modules = set(self.modules)
        if len(modules) != 2 or {item.module for item in self.viewpoints} != modules:
            raise ValueError("Both distinct selected modules must be represented")
        item_ids = {item.item_id for item in self.viewpoints}
        if len(item_ids) != 20:
            raise ValueError("Twenty distinct viewpoint identities are required")
        if len(set(self.draft_ids)) != len(self.draft_ids):
            raise ValueError("Draft identities must be unique")
        attempts = {(attempt.task_key, attempt.run_count) for attempt in self.run_attempts}
        if len(attempts) != len(self.run_attempts):
            raise ValueError("Task attempt identities must be unique")
        if any(attempt.item_id not in item_ids for attempt in self.run_attempts):
            raise ValueError("Every task attempt must identify a selected viewpoint")
        return self

    def selection_digest(self) -> str:
        payload = self.model_dump(mode="json", exclude={"timings"})
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class DraftObservation(ContractModel):
    draft_id: PositiveId
    status: Literal["running", "pending", "approved", "rejected", "error", "cancelled"]
    generated_item_ids: list[PositiveId] = Field(default_factory=list, max_length=20)
    accepted_item_ids: list[PositiveId] = Field(default_factory=list, max_length=20)
    conversion_item_ids: list[PositiveId] = Field(default_factory=list, max_length=20)
    provider_kind: Literal["live", "stub", "unknown"] = "unknown"
    issues: list[Reference] = Field(default_factory=list, max_length=100)


class RunObservation(ContractModel):
    task_key: Annotated[str, Field(strict=True, pattern=r"^T[0-9]{1,15}$")]
    run_count: PositiveId
    item_id: PositiveId
    verified: bool = False
    finalised: bool = False
    evidence_kind: Literal["silver_runtime", "synthetic", "unclassified"] = "unclassified"
    runner_backend: Literal["silver", "mock", "unknown"] = "unknown"
    status: Literal["queued", "running", "passed", "failed", "cancelled"] = "queued"
    verdict: Annotated[str, Field(strict=True, max_length=32)] = ""
    issues: list[Reference] = Field(default_factory=list, max_length=100)


class AuditSnapshot(ContractModel):
    selection_sha256: Digest
    source: Literal["postgresql_readonly", "unverified"] = "unverified"
    preflight_issues: list[Reference] = Field(default_factory=list, max_length=100)
    drafts: list[DraftObservation] = Field(default_factory=list, max_length=100)
    runs: list[RunObservation] = Field(default_factory=list, max_length=100)
