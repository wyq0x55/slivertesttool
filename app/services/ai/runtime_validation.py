"""Validate draft procedures through the production exporter and runner parser."""

from __future__ import annotations

from types import SimpleNamespace

from ..lanmatrix import silver_json_export as exporter
from ..run_validation_service import ConversionError


def _row(values, *, case_id="AI-DRAFT"):
    return SimpleNamespace(case_id=values.get("case_id") or case_id, get_field=values.get)


def validate_runtime(payload, documents, *, candidate=None) -> list[str]:
    try:
        inputs = payload.get("runtime_inputs", {"constants": [], "libraries": []})
        constants = [_row(values) for values in inputs["constants"]]
        libraries = [_row(values) for values in inputs["libraries"]]
        if candidate is not None:
            parameters = candidate.get("lib_para") or []
            encoded = [parameter["name"] + ("=" + str(parameter["default"])
                       if parameter.get("default") is not None else "") for parameter in parameters]
            libraries.append(_row({"lib_func": candidate["lib_name"], "lib_stb": candidate["lib_stb"],
                                   "lib_para": "\n".join(encoded)}))
            documents = [*documents, {"steps": [{"no": 1, "subroutine": candidate["lib_name"],
                                                "args": ["0"] * len(parameters)}]}]
        problems = []
        for position, document in enumerate(documents, 1):
            try:
                exporter.build_documents(_row({"steps": document}), constants, libraries)
            except (ConversionError, ValueError, TypeError, KeyError, AttributeError) as exc:
                problems.append(f"Runner conversion document {position}: {exc}")
        return problems
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        return [f"Runner input snapshot is invalid: {exc}"]
