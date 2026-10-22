"""Manage uploaded/form-authored manifests without owning their semantics.

The external ut-agent validates schema and versioned references. Each accepted
document receives a content-addressed file; a subsequent save never rewrites a
manifest referenced by a queued batch.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from ..lanmatrix.errors import ServiceError
from .bridge import BridgeError

MAX_MANIFEST_BYTES = 256 * 1024


def _encoded(document: object) -> bytes:
    if not isinstance(document, dict):
        raise ServiceError("ProjectManifest 必须是 JSON 对象", code="VALIDATION_ERROR")
    try:
        data = (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2,
                           allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ServiceError("ProjectManifest 包含无效的 JSON 值", code="VALIDATION_ERROR") from exc
    if len(data) > MAX_MANIFEST_BYTES:
        raise ServiceError("ProjectManifest 不能超过 256 KiB", code="VALIDATION_ERROR")
    return data


def prepare_manifest(project_id: int, document: object, *, bridge, workspace: Path,
                     persist: bool) -> dict:
    data = _encoded(document)
    digest = hashlib.sha256(data).hexdigest()
    directory = workspace / f"p{project_id}" / "manifests"
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".validate-", dir=directory) as staging:
        candidate = Path(staging) / "manifest.json"
        candidate.write_bytes(data)
        try:
            constraints = bridge.constraints(str(candidate))
        except BridgeError as exc:
            raise ServiceError(f"项目清单校验失败：{exc}", code="UT_MANIFEST_INVALID") from exc
        result = {"sha256": digest, "baseline": constraints.get("baseline"),
                  "provenance": constraints.get("provenance")}
        if persist:
            destination = directory / f"{digest}.json"
            try:
                # Publish a complete validated file without replacing a prior revision.
                os.link(candidate, destination)
            except FileExistsError:
                if destination.is_symlink() or destination.read_bytes() != data:
                    raise ServiceError("已保存的项目清单内容与其版本标识不符，请检查存储", code="UT_MANIFEST_INTEGRITY")
            result["path"] = str(destination.resolve())
    return result


def read_manifest(path: str, *, bridge) -> dict:
    """Read only a bound document that the authoritative validator accepts."""
    file = Path(path)
    if file.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("ProjectManifest 不能超过 256 KiB")
    # Legacy path bindings are user-selected server paths. Validate before
    # exposing their JSON so this endpoint cannot become a generic file reader.
    bridge.constraints(str(file))
    with file.open("rb") as stream:
        data = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(data) > MAX_MANIFEST_BYTES:
        raise ValueError("ProjectManifest 不能超过 256 KiB")
    document = json.loads(data.decode("utf-8-sig"))
    if not isinstance(document, dict):
        raise TypeError("ProjectManifest 必须是 JSON 对象")
    return {"manifest_document": document, "manifest_sha256": hashlib.sha256(data).hexdigest()}
