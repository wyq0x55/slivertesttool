"""Assemble reproducible, project-scoped generation inputs on the server."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ...extensions import db
from ...models import ProjectModel, Task, TestItemRow
from ..lanmatrix import sbs_service
from . import signal_dict

MAX_SELECTED = 200
MAX_SOURCE_CHARS = 256_000
_SAFE_COMPILER_ARG = re.compile(r"(?:-D[A-Za-z_]\w*(?:=[^\r\n\x00]{0,256})?|-U[A-Za-z_]\w*|-std=(?:c|gnu)(?:89|90|99|11|17|18|23))\Z")


def _digest(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _selected(project_id: int, ids: Any) -> list[TestItemRow]:
    if (not isinstance(ids, list) or not ids or len(ids) > MAX_SELECTED
            or any(type(item_id) is not int or item_id < 1 for item_id in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError(f"item_ids must contain 1-{MAX_SELECTED} distinct positive integers")
    rows = TestItemRow.query.filter(
        TestItemRow.project_id == project_id, TestItemRow.id.in_(ids),
        TestItemRow.sheet == "test", TestItemRow.deleted_at.is_(None),
    ).all()
    by_id = {row.id: row for row in rows}
    if len(by_id) != len(ids):
        raise ValueError("Selected test rows are missing, deleted, or outside this project")
    return [by_id[item_id] for item_id in ids]


def _selection_ids(payload: dict, key: str) -> Any:
    if "item_ids" in payload:
        return payload["item_ids"]
    entries = payload.get(key)
    if isinstance(entries, list):
        return [entry.get("item_id") if isinstance(entry, dict) else None for entry in entries]
    return [payload.get("item_id")]


def _viewpoint(row: TestItemRow) -> dict:
    return {
        "ref": str(row.id), "item_id": row.id, "version": row.version,
        "case_id": row.get_field("test_id") or row.case_id,
        "title": row.title, "module": row.module or "",
        "precondition": row.precondition,
        "condition": row.get_field("purpose") or "",
        "expected": row.expected_result or row.get_field("description") or "",
        "viewpoint": row.get_field("viewpoint") or "",
    }


def _steps(row: TestItemRow, key: str = "steps") -> dict:
    value = row.get_field(key)
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value) if value else {}
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _sources(payload: dict, provenance: list[dict]) -> None:
    for key, kind in (("doc_text", "submitted_document"), ("log_text", "submitted_log"),
                      ("source_context", "submitted_context")):
        value = payload.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or len(value) > MAX_SOURCE_CHARS:
            raise ValueError(f"{key} must be text of at most {MAX_SOURCE_CHARS} characters")
        provenance.append({"kind": kind, "name": str(payload.get("source_name") or key)[:256],
                           "revision": str(payload.get("source_revision") or "")[:256],
                           "sha256": _digest(value)})
    files = payload.get("source_files") or {}
    if (not isinstance(files, dict) or len(files) > 32
            or any(not isinstance(name, str) or not isinstance(content, str)
                   for name, content in files.items())
            or sum(len(content) for content in files.values()) > MAX_SOURCE_CHARS):
        raise ValueError("source_files must contain at most 32 bounded text excerpts")
    for name, content in files.items():
        provenance.append({"kind": "submitted_source", "name": name[:256],
                           "revision": str(payload.get("source_revision") or "")[:256],
                           "sha256": _digest(content)})


def _failure_context(project_id, row, payload, provenance):
    from ..run_evidence_service import read_evidence

    key = payload.get("task_key")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("Failure analysis requires an archived task_key")
    task = Task.query.filter_by(project_id=project_id, task_key=key, deleted_at=None).first()
    if task is None:
        raise ValueError("Archived task is unavailable in this project")
    data = read_evidence(task, payload.get("run_count"))
    if data["outcome"] is None:
        raise ValueError("Failure analysis requires sealed run evidence")
    approved = data["approved_inputs"]
    saved_row = approved.get("row") or {}
    if saved_row.get("id") != row.id or saved_row.get("uuid") != row.uuid or not isinstance(approved.get("test"), dict):
        raise ValueError("Archived approved inputs do not belong to the selected matrix row")
    payload["task_key"] = task.task_key
    payload["run_count"] = data["run_count"]
    payload["viewpoint"] = {key: saved_row.get(key) or "" for key in ("title", "module", "precondition")}
    payload["viewpoint"].update(item_id=row.id, version=saved_row["version"],
                                case_id=saved_row.get("test_id") or saved_row["case_id"],
                                condition=saved_row.get("purpose") or "",
                                expected=saved_row.get("expected_result") or saved_row.get("description") or "")
    payload["steps_doc"] = approved["test"]
    payload["log_text"] = json.dumps(data["outcome"], ensure_ascii=False) + "\n" + "\n".join(
        f"{name}\n{content}" for name, content in data["logs"].items())
    model = {key: data["model"].get(key) for key in ("id", "name", "version", "sha256")}
    provenance.append({"kind": "run_attempt", "task_key": task.task_key, "run_count": data["run_count"],
                       "evidence_kind": data["outcome"].get("evidence_kind", "unclassified"),
                       "sha256": _digest({key: data[key] for key in ("approved_inputs", "model", "outcome", "artifacts", "input_files")}),
                       "artifacts": data["artifacts"]})
    return model


def build_payload(project_id: int, scenario: str, submitted: dict[str, Any]) -> dict[str, Any]:
    payload = dict(submitted)
    payload.pop("_context", None)
    if scenario == "failure":
        payload.pop("log_text", None)
    arguments = payload.get("compile_args", [])
    if (not isinstance(arguments, list) or len(arguments) > 64
            or any(not isinstance(argument, str) or not _SAFE_COMPILER_ARG.fullmatch(argument)
                   for argument in arguments)):
        raise ValueError("compile_args supports bounded -D/-U definitions and C language standards only")
    provenance: list[dict] = []
    if "doc" in payload and "doc_text" not in payload:
        payload["doc_text"] = payload.pop("doc")
    _sources(payload, provenance)
    selected: list[TestItemRow] = []
    if scenario in ("procedure", "lib"):
        selected = _selected(project_id, _selection_ids(payload, "viewpoints" if scenario == "procedure" else "procedures"))
    elif scenario == "failure":
        selected = _selected(project_id, [payload.get("item_id")])
    for key in ("viewpoint", "viewpoints", "procedures", "steps_doc"):
        payload.pop(key, None)
    for row in selected:
        provenance.append({"kind": "matrix_row", "id": row.id, "version": row.version,
                           "sha256": _digest(row.to_dict())})

    rows = (TestItemRow.query.filter_by(project_id=project_id, deleted_at=None)
            .order_by(TestItemRow.id).all())
    libraries = [row for row in rows if row.sheet == "lib"]
    payload["runtime_inputs"] = {
        "constants": [{key: row.get_field(key) for key in ("const_name", "const_value", "const_jname", "const_note")}
                      for row in rows if row.sheet == "const"],
        "libraries": [{"case_id": row.case_id, **{key: row.get_field(key)
                       for key in ("lib_func", "lib_name", "isinit", "lib_stb", "lib_para")}}
                      for row in libraries],
    }
    payload["lib_functions"] = [
        {"name": row.get_field("lib_func") or row.get_field("lib_name") or row.case_id or "", "item_id": row.id,
         "version": row.version, "params": row.get_field("lib_para") or "",
         "steps_doc": _steps(row, "lib_stb")} for row in libraries
    ]
    payload["existing_lib_names"] = [entry["name"] for entry in payload["lib_functions"] if entry["name"]]
    payload["constant_names"] = [row.get_field("const_name") for row in rows if row.sheet == "const" and row.get_field("const_name")]
    payload["signal_dict"] = signal_dict.entries_for(project_id)
    payload["sbs_variables"] = [[row.get_field("io_name") or row.get_field("io_path"),
                                 row.get_field("io_path")]
                                for row in rows if row.sheet == "io" and row.get_field("io_path")]
    history = []
    dependencies = [row for row in rows if row.sheet in ("const", "lib", "io")]
    for row in rows:
        if row.sheet != "test" or row.workflow_status == "Draft":
            continue
        doc = _steps(row)
        dependencies.append(row)
        for key in ("input_signals", "expected_signals"):
            if isinstance(doc.get(key), list):
                history.extend(doc[key])
    payload["historical_pairs"] = history
    provenance.append({"kind": "project_signals", "project_id": project_id,
                       "sha256": _digest({key: payload[key] for key in ("signal_dict", "sbs_variables", "historical_pairs")})})

    model_id = payload.get("model_id")
    if model_id is not None and (type(model_id) is not int or model_id < 1):
        raise ValueError("model_id must be a positive integer")
    model = None if scenario == "failure" else (db.session.get(ProjectModel, model_id) if model_id else
             ProjectModel.query.filter_by(project_id=project_id, is_current=True, deprecated_at=None).first())
    if scenario != "failure" and model_id and (model is None or model.project_id != project_id or model.deprecated_at is not None):
        raise ValueError("Saved model is unavailable in this project")
    model_context = None
    payload["current_sbs"] = ""
    payload["sbs_text"] = ""
    if model is not None:
        model_context = {"id": model.id, "name": model.name, "version": model.version or ""}
        payload["model_id"] = model.id
        if model.kind == "bundle":
            saved = sbs_service.read_sbs(project_id, model.name, model_id=model.id)
            payload["current_sbs"] = saved["content"]
            payload["sbs_text"] = saved["content"]
            model_context["sbs_sha256"] = saved["version"]
        provenance.append({"kind": "saved_model", **model_context})
    if scenario == "sbs" and (model_context is None or "sbs_sha256" not in model_context):
        raise ValueError("SBS proposals require a saved bundle model")

    if scenario == "procedure":
        payload.pop("viewpoint", None)
        payload["viewpoints"] = [_viewpoint(row) for row in selected]
    elif scenario == "lib":
        payload["procedures"] = [{"item_id": row.id, "version": row.version,
                                  "steps_doc": _steps(row)} for row in selected]
    elif scenario == "failure":
        model_context = _failure_context(project_id, selected[0], payload, provenance)
        payload.pop("model_id", None)
    payload["_context"] = {"schema_version": 1, "project_id": project_id,
                           "items": [{"id": row.id, "version": row.version} for row in selected],
                           "dependencies": [{"id": row.id, "version": row.version} for row in dependencies],
                           "signal_dict_sha256": _digest(payload["signal_dict"]),
                           "model": model_context, "provenance": provenance}
    return payload
