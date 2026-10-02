"""Pinned inputs and sealed results keyed by the existing task attempt identity."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path


class EvidenceError(ValueError):
    """Evidence is missing, unsafe, changed, or belongs to a different attempt."""


def validate_segment(value):
    value = str(value or "")
    if (not value or value in (".", "..") or value != value.strip(" .")
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', value)
            or value.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(
                f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10))}):
        raise EvidenceError("Unsafe filesystem identity")
    return value


def checked_path(root, path, *, exists=False):
    """Require lexical and resolved containment and reject every reparse component."""
    root = Path(os.path.abspath(root))
    path = Path(os.path.abspath(path))
    try:
        ancestors = list(reversed(root.parents)) + [root]
        ancestors.extend(list(reversed(path.parents)) + [path])
        for current in ancestors:
            if current.exists() or current.is_symlink():
                info = current.lstat()
                if current.is_symlink() or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024):
                    raise EvidenceError(f"Reparse path is not allowed: {current}")
        resolved = path.resolve(strict=exists)
        resolved.relative_to(root.resolve(strict=exists))
    except (OSError, RuntimeError, ValueError) as exc:
        raise EvidenceError(f"Path containment failed: {path}") from exc
    return resolved


def _task(value):
    if isinstance(value, str):
        from ..models import Task
        value = Task.query.filter_by(task_key=value, deleted_at=None).first()
    if value is None or getattr(value, "deleted_at", None) is not None:
        raise EvidenceError("Task is missing or deleted")
    return value


def attempt_dir(task, run_count=None, *, workspace=None):
    task = _task(task)
    base = Path(workspace or task.workspace)
    if not str(workspace or task.workspace or ""):
        raise EvidenceError("Task workspace is missing")
    validate_segment(task.test_id)
    key = validate_segment(task.task_key)
    attempt = (task.run_count or 1) if run_count is None else run_count
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise EvidenceError("Invalid run_count")
    return checked_path(base, base / ".evidence" / key / str(attempt))


def _digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _files(root):
    checked_path(root, root, exists=True)
    for path in sorted(root.rglob("*")):
        checked_path(root, path, exists=True)
        if path.is_file():
            yield path


def _hashes(root):
    return {path.relative_to(root).as_posix(): _digest(path) for path in _files(root)}


def _write_json(path, data):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())


def _read_json(root, name):
    path = checked_path(root, root / name, exists=True)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EvidenceError(f"Cannot read evidence {name}") from exc


def _identity(task, run_count):
    from ..extensions import db
    from ..models import RunEvidence, Task
    return db.session.get(RunEvidence, (task.id, run_count)) if isinstance(task, Task) else None


def _check_identity(task, root, run_count, *, outcome=False):
    from ..models import Task
    if not isinstance(task, Task):
        return
    record = _identity(task, run_count)
    name = "outcome.json" if outcome else "manifest.json"
    expected = (record.outcome_sha256 if outcome else record.manifest_sha256) if record else None
    if not expected or _digest(checked_path(root, root / name, exists=True)) != expected:
        raise EvidenceError(f"Evidence {name} hash integrity does not match the committed attempt")


def _pin_model(task, source, root, workspace):
    ref = Path(task.sil_relpath)
    model = ref if ref.is_absolute() else source / ref
    model = checked_path(model.parent, model, exists=True)
    if not model.is_file():
        raise EvidenceError("Saved model is missing")
    model_root = model.parent
    original = model.read_bytes()
    execution_text = original
    source_path = str(model)
    if model_root.name == "model" and ".evidence" in model_root.parts:
        previous_root = checked_path(workspace, model_root.parent, exists=True)
        try:
            previous_count = int(previous_root.name)
        except ValueError as exc:
            raise EvidenceError("Previous pinned model attempt identity is invalid") from exc
        if (previous_count >= (task.run_count or 1)
                or previous_root != attempt_dir(task, previous_count, workspace=workspace)):
            raise EvidenceError("Previous pinned model attempt identity mismatch")
        previous = _manifest(task, previous_root, previous_count)["model"]
        original = checked_path(model_root, model_root / "approved.sil", exists=True).read_bytes()
        source_path = previous["source_path"]
    target_root = root / "model"
    target_root.mkdir()
    (target_root / "approved.sil").write_bytes(original)
    executable = target_root / model.name
    if executable.name == "approved.sil":
        executable = target_root / "execution.sil"
    try:
        text = execution_text.decode("utf-8")
    except UnicodeError as exc:
        raise EvidenceError("Only text saved .sil configurations can be pinned safely") from exc
    from .project_model_service import ModelError, rewrite_model_module_paths
    from ..config import BASE_DIR

    def replace(match):
        raw = match.group("path")
        ref = Path(raw)
        candidates = [ref] if ref.is_absolute() else [Path(BASE_DIR) / ref, model_root / ref]
        original_path = next((candidate for candidate in candidates if candidate.is_file()), None)
        if original_path is None:
            raise EvidenceError(f"Saved model dependency is missing: {raw}")
        original_path = checked_path(model_root, original_path, exists=True)
        destination = target_root / validate_segment(original_path.name)
        content = original_path.read_bytes()
        if destination.exists() and destination.read_bytes() != content:
            raise EvidenceError("Saved model dependency name collision")
        destination.write_bytes(content)
        if original_path.suffix.lower() == ".dll":
            symbols = original_path.with_suffix(".pdb")
            if symbols.is_file():
                checked_path(model_root, symbols, exists=True)
                shutil.copy2(symbols, target_root / symbols.name)
        return destination.resolve().as_posix()

    try:
        execution = rewrite_model_module_paths(text, replace)
    except ModelError as exc:
        raise EvidenceError(str(exc)) from exc
    executable.write_bytes(execution.encode("utf-8"))
    return {"id": getattr(task, "sil_model_id", None), "name": task.sil_name or "",
            "version": task.sil_version or "", "source_path": source_path,
            "sha256": hashlib.sha256(original).hexdigest(),
            "execution_path": str(executable.resolve()), "files": _hashes(target_root)}


def pin_attempt(task, source_dir, *, approved_inputs=None, workspace=None):
    """Pin once before the submission commit; does not commit, enqueue, or execute."""
    task = _task(task)
    root = attempt_dir(task, workspace=workspace)
    source = Path(source_dir)
    workspace = Path(workspace or task.workspace)
    checked_path(workspace, source, exists=True)
    for _path in _files(source):
        pass
    root.parent.mkdir(parents=True, exist_ok=True)
    from ..models import Task
    if root.exists() and isinstance(task, Task) and _identity(task, task.run_count or 1) is None:
        orphan = checked_path(workspace, root.parent / f"aborted-{task.run_count or 1}-{uuid.uuid4().hex}")
        checked_path(workspace, root, exists=True)
        root.rename(orphan)
    root.mkdir()
    try:
        inputs = root / "inputs"
        inputs.mkdir()
        shutil.copytree(source, inputs / validate_segment(task.test_id))
        model = _pin_model(task, source, root, workspace)
        manifest = {"schema": 1, "task_id": task.id, "task_key": task.task_key,
                    "run_count": task.run_count or 1, "project_id": task.project_id,
                    "test_id": task.test_id, "created_at": datetime.now(timezone.utc).isoformat(),
                    "approved_inputs": approved_inputs or {}, "input_files": _hashes(inputs),
                    "model": model}
        _write_json(root / "manifest.json", manifest)
        from ..extensions import db
        from ..models import RunEvidence, Task
        if isinstance(task, Task):
            db.session.add(RunEvidence(task_id=task.id, run_count=task.run_count or 1,
                                        manifest_sha256=_digest(root / "manifest.json")))
    except Exception:
        checked_path(workspace, root, exists=True)
        shutil.rmtree(root)
        raise
    task.workspace = str(workspace)
    task.report_path = str(root)
    task.sil_relpath = model["execution_path"]
    return root


def _manifest(task, root, run_count):
    _check_identity(task, root, run_count)
    manifest = _read_json(root, "manifest.json")
    expected = (task.id, task.task_key, run_count, task.project_id, task.test_id)
    actual = tuple(manifest.get(key) for key in ("task_id", "task_key", "run_count", "project_id", "test_id"))
    if manifest.get("schema") != 1 or actual != expected:
        raise EvidenceError("Evidence attempt identity mismatch")
    for directory, hashes in (("inputs", manifest["input_files"]), ("model", manifest["model"]["files"])):
        if _hashes(root / directory) != hashes:
            raise EvidenceError(f"Evidence {directory} hash integrity check failed")
    execution_path = checked_path(root / "model", Path(manifest["model"]["execution_path"]), exists=True)
    if not execution_path.is_file():
        raise EvidenceError("Pinned model is missing")
    return manifest


def read_evidence(task, run_count=None, *, max_log_bytes=262144):
    """Server-side schema-1 context; callers must enforce project permissions."""
    if type(max_log_bytes) is not int or not 0 <= max_log_bytes <= 1_048_576:
        raise EvidenceError("Log budget must be between zero and 1048576 bytes")
    task = _task(task)
    attempt = (task.run_count or 1) if run_count is None else run_count
    root = attempt_dir(task, attempt)
    manifest = _manifest(task, root, attempt)
    documents = {}
    case = root / "inputs" / task.test_id
    for name in ("constants.json", "lib.json", *[path.name for path in case.glob("testcase_*.json")]):
        if (case / name).is_file():
            documents[name] = _read_json(case, name)
    outcome = None
    artifacts = []
    logs = {}
    remaining = max_log_bytes
    if (root / "outcome.json").exists():
        _check_identity(task, root, attempt, outcome=True)
        outcome = _read_json(root, "outcome.json")
        if _hashes(root / "results") != outcome["result_files"]:
            raise EvidenceError("Result archive hash integrity check failed")
        for path in _files(root / "results"):
            artifacts.append({"path": path.relative_to(root).as_posix(),
                              "sha256": outcome["result_files"][path.relative_to(root / "results").as_posix()],
                              "size": path.stat().st_size})
            if remaining and path.suffix.lower() in (".log", ".txt"):
                with path.open("rb") as stream:
                    stream.seek(max(0, path.stat().st_size - remaining))
                    content = stream.read(remaining)
                    remaining -= len(content)
                    logs[path.relative_to(root / "results").as_posix()] = content.decode("utf-8", errors="replace")
    return {**manifest, "archive_path": str(root), "documents": documents,
            "outcome": outcome, "artifacts": artifacts, "logs": logs}


def seal_attempt(task, *, status, verdict, message, evidence_kind="unclassified", runner_backend=""):
    """Seal an outcome once, including runner errors and cancellation logs."""
    root = attempt_dir(task)
    _manifest(task, root, task.run_count or 1)
    results = checked_path(root, root / "results")
    results.mkdir(exist_ok=True)
    _write_json(root / "outcome.json", {
        "status": status, "verdict": verdict, "message": message,
        "evidence_kind": evidence_kind,
        "runner_backend": runner_backend,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "result_files": _hashes(results),
    })
    record = _identity(task, task.run_count or 1)
    if record is not None:
        record.outcome_sha256 = _digest(root / "outcome.json")


def read_record_evidence(record, *, max_log_bytes=262144):
    from ..models import Task
    if not getattr(record, "run_count", None):
        raise EvidenceError("Legacy run record has no pinned attempt identity")
    task = Task.query.filter_by(task_key=record.task_key, project_id=record.project_id,
                               deleted_at=None).first()
    return read_evidence(task, record.run_count, max_log_bytes=max_log_bytes)


def recover_queued_attempts(enqueue, *, is_pending, limit=100):
    """Bounded at-least-once dispatch of approved, pinned queued attempts only.

    Both callbacks accept (task_id, run_count). Queue ownership must use this
    attempt identity for deduplication and atomic worker claims. The callback
    must enqueue asynchronously, not execute a runner in this transaction.
    """
    from .run_recovery_service import recover_queued_attempts as recover

    return recover(enqueue, is_pending=is_pending, limit=limit)
