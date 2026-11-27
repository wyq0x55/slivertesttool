"""Store unit-test source uploaded by a client.

Distributed clients do not share the execution server's filesystem. A project
therefore keeps its Product/source tree and function index inside
``WORKSPACE_DIR/ut_sources/p<id>`` and points ``product_root`` / ``index_csv``
at that private copy. The rest of the pipeline keeps reading those two paths.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import zipfile
from io import BytesIO
from pathlib import Path

from flask import current_app
from werkzeug.datastructures import FileStorage

from ...extensions import db
from ...models import UtPipelineConfig
from ..lanmatrix import audit
from ..lanmatrix.errors import ServiceError
from ..run_evidence_service import EvidenceError, checked_path

_MAX_MEMBERS = 10_000
_MAX_INDEX_BYTES = 32 * 1024 * 1024
_BAD_PART = re.compile(r'[<>:"|?*\x00-\x1f]')
_META_NAME = "upload.json"


def _workspace() -> Path:
    raw = current_app.config.get("WORKSPACE_DIR")
    if not raw:
        raise ServiceError("未配置工作区，无法保存上传的源码。", code="VALIDATION_ERROR")
    return Path(os.path.abspath(raw))


def sandbox_for(project_id: int, *, create: bool) -> Path:
    """Per-project directory that holds uploaded source, never a shared drive."""
    root = _workspace()
    sandbox = root / "ut_sources" / f"p{int(project_id)}"
    try:
        checked_path(root, sandbox, exists=False)
    except EvidenceError as exc:
        raise ServiceError("项目源码目录无效。", code="VALIDATION_ERROR") from exc
    if create:
        sandbox.mkdir(parents=True, exist_ok=True)
    return sandbox


def upload_status(project_id: int) -> dict | None:
    """Names and counts recorded for the last successful upload, if any."""
    try:
        sandbox = sandbox_for(project_id, create=False)
    except ServiceError:
        return None
    path = sandbox / _META_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    source_files = data.get("source_files")
    return {
        "source_name": str(data.get("source_name") or ""),
        "source_files": source_files if isinstance(source_files, int) else 0,
        "index_name": str(data.get("index_name") or ""),
    }


def _limit() -> int:
    raw = current_app.config.get("MAX_UPLOAD_BYTES") or (512 * 1024 * 1024)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 512 * 1024 * 1024


def _read_limited(storage: FileStorage, limit: int, label: str) -> bytes:
    chunks = []
    total = 0
    while True:
        block = storage.stream.read(min(1024 * 1024, limit - total + 1))
        if not block:
            break
        total += len(block)
        if total > limit:
            raise ServiceError(f"{label}超过大小限制。", code="VALIDATION_ERROR")
        chunks.append(block)
    if total == 0:
        raise ServiceError(f"请上传非空的{label}。", code="VALIDATION_ERROR")
    return b"".join(chunks)


def _member_relative(name: str) -> Path | None:
    raw = (name or "").replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise ServiceError("压缩包包含绝对路径。", code="VALIDATION_ERROR")
    parts = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == ".." or _BAD_PART.search(part) or part.endswith((" ", ".")):
            raise ServiceError(f"压缩包包含不安全的路径：{name}", code="VALIDATION_ERROR")
        parts.append(part)
    if not parts:
        return None
    return Path(*parts)


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(mode)


def _extract_zip(payload: bytes, staging: Path, limit: int) -> int:
    if not payload.startswith(b"PK"):
        raise ServiceError("源码必须是 zip 压缩包。", code="VALIDATION_ERROR")
    try:
        archive = zipfile.ZipFile(BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise ServiceError("源码压缩包无法读取。", code="VALIDATION_ERROR") from exc
    count = 0
    written_total = 0
    with archive:
        for info in archive.infolist():
            if info.is_dir() or info.filename.endswith("/"):
                _member_relative(info.filename)
                continue
            if _is_symlink(info):
                raise ServiceError("压缩包不能包含符号链接。", code="VALIDATION_ERROR")
            relative = _member_relative(info.filename)
            if relative is None:
                continue
            count += 1
            if count > _MAX_MEMBERS:
                raise ServiceError("压缩包内的文件过多。", code="VALIDATION_ERROR")
            if info.file_size > limit or written_total + max(0, info.file_size) > limit:
                raise ServiceError("解压后的源码超过大小限制。", code="VALIDATION_ERROR")
            destination = staging / relative
            try:
                checked_path(staging, destination, exists=False)
            except EvidenceError as exc:
                raise ServiceError("压缩包包含越界路径。", code="VALIDATION_ERROR") from exc
            destination.parent.mkdir(parents=True, exist_ok=True)
            remaining = limit - written_total
            written = 0
            with archive.open(info, "r") as source, destination.open("wb") as target:
                while True:
                    block = source.read(min(1024 * 1024, max(1, remaining - written + 1)))
                    if not block:
                        break
                    if written + len(block) > remaining:
                        raise ServiceError("解压后的源码超过大小限制。", code="VALIDATION_ERROR")
                    target.write(block)
                    written += len(block)
            written_total += written
    if count == 0:
        raise ServiceError("压缩包里没有源码文件。", code="VALIDATION_ERROR")
    return count


def _replace_tree(staging: Path, destination: Path) -> None:
    previous = destination.parent / ".source-previous"
    if previous.exists():
        shutil.rmtree(previous)
    moved = False
    try:
        if destination.exists():
            destination.rename(previous)
            moved = True
        staging.rename(destination)
    except Exception:
        if moved and previous.exists() and not destination.exists():
            previous.rename(destination)
        raise
    if previous.exists():
        shutil.rmtree(previous, ignore_errors=True)


def _write_meta(sandbox: Path, **changes) -> dict:
    path = sandbox / _META_NAME
    current = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                current = loaded
        except (OSError, UnicodeError, ValueError):
            current = {}
    current.update(changes)
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return upload_status(int(sandbox.name[1:])) or {
        "source_name": str(current.get("source_name") or ""),
        "source_files": int(current.get("source_files") or 0),
        "index_name": str(current.get("index_name") or ""),
    }


def _assign(config: UtPipelineConfig, *, product_root: str | None, index_csv: str | None,
            user_id: int) -> None:
    before = {"product_root": config.product_root, "index_csv": config.index_csv}
    if product_root is not None:
        if len(product_root) > 1024:
            raise ServiceError("保存后的源码路径过长。", code="VALIDATION_ERROR")
        config.product_root = product_root
    if index_csv is not None:
        if len(index_csv) > 1024:
            raise ServiceError("保存后的索引路径过长。", code="VALIDATION_ERROR")
        config.index_csv = index_csv
    config.updated_by = user_id
    db.session.add(config)
    audit.record(
        "ut.source.upload",
        actor_id=user_id,
        object_type="ut_config",
        object_id=config.project_id,
        project_id=config.project_id,
        old_value=before,
        new_value={"product_root": config.product_root, "index_csv": config.index_csv},
    )


def store_sources(project_id: int, *, source: FileStorage | None, index: FileStorage | None,
                  user_id: int) -> dict:
    """Replace the project's uploaded source tree and/or function index."""
    has_source = source is not None and bool(getattr(source, "filename", ""))
    has_index = index is not None and bool(getattr(index, "filename", ""))
    if not has_source and not has_index:
        raise ServiceError("请上传源码压缩包或函数索引 CSV。", code="VALIDATION_ERROR")
    if has_source and not str(source.filename).lower().endswith(".zip"):
        raise ServiceError("源码必须是 .zip 压缩包。", code="VALIDATION_ERROR")
    if has_index and not str(index.filename).lower().endswith(".csv"):
        raise ServiceError("函数索引必须是 .csv 文件。", code="VALIDATION_ERROR")

    sandbox = sandbox_for(project_id, create=True)
    limit = _limit()
    product_root = None
    index_csv = None
    meta_changes = {}
    staging = sandbox / ".source-new"
    if has_source:
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir()
        try:
            count = _extract_zip(_read_limited(source, limit, "源码压缩包"), staging, limit)
            _replace_tree(staging, sandbox / "source")
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        product_root = str((sandbox / "source").resolve())
        meta_changes["source_name"] = Path(str(source.filename).replace("\\", "/")).name
        meta_changes["source_files"] = count
    if has_index:
        payload = _read_limited(index, min(limit, _MAX_INDEX_BYTES), "函数索引")
        target = sandbox / "index.csv"
        temporary = sandbox / ".index-new"
        temporary.write_bytes(payload)
        os.replace(temporary, target)
        index_csv = str(target.resolve())
        meta_changes["index_name"] = Path(str(index.filename).replace("\\", "/")).name

    config = UtPipelineConfig.query.filter_by(project_id=project_id).first()
    if config is None:
        config = UtPipelineConfig(
            project_id=project_id,
            max_repair_rounds=current_app.config.get("UT_REPAIR_MAX_ROUNDS", 5),
        )
    _assign(config, product_root=product_root, index_csv=index_csv, user_id=user_id)
    status = _write_meta(sandbox, **meta_changes)
    db.session.commit()
    return {
        "product_root": config.product_root,
        "index_csv": config.index_csv,
        "source_upload": status,
    }
