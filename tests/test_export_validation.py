from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.services.lanmatrix import silver_json_export as exporter


def row(**values):
    return SimpleNamespace(case_id="TC-1", get_field=values.get)


def body(**changes):
    result = {
        "input_signals": [["in", "IN"]],
        "expected_signals": [["out", "OUT"]],
        "steps": [{"no": 1, "inputs": ["BASE + 2"],
                   "expecteds": ["[BASE, 10)"], "timing": "100ms以内"}],
    }
    result.update(changes)
    return result


def test_valid_export_uses_actual_runner_parser(tmp_path):
    from app.services.run_validation_service import validate_documents

    manifest = exporter.materialise_run_dir(
        tmp_path / "TC-1", row(steps=body()),
        [row(const_name="BASE", const_value="0x2")], [])
    case = json.loads(manifest["testcase_json"].read_text(encoding="utf-8"))
    constants = json.loads(manifest["constants_json"].read_text(encoding="utf-8"))
    library = json.loads(manifest["lib_json"].read_text(encoding="utf-8"))
    steps = validate_documents(case, constants, library)
    assert steps[0].inputs[0].value == 4
    assert steps[0].checks[0].exp_value == (2, 10, True, False)
    assert steps[0].timeout == 0.1


@pytest.mark.parametrize("procedure,libraries,constants", [
    ("{broken", [], []),
    (body(steps=[]), [], []),
    (body(steps=[{"no": 1, "inputs": ["UNKNOWN"]}]), [], []),
    (body(steps=[{"no": 1, "expecteds": ["[10, 2]"]}]), [], []),
    (body(steps=[{"no": 1, "expecteds": ["1"], "timing": "-10ms以内"}]), [], []),
    (body(default_timeout=float("inf")), [], []),
    (body(steps=[{"no": 1, "expecteds": ["1"], "timing": "unknown 10ms"}]), [], []),
    (body(steps=[{"no": 1, "expecteds": ["1"], "timing": "-.5ms以内"}]), [], []),
    (body(steps=[{"no": 1, "inputs": ["1", "2"]}]), [], []),
    (body(input_signals=[["in", ""]]), [], []),
    (body(steps=[{"no": 1, "subroutine": "Missing"}]), [], []),
    (body(steps=[{"no": 1, "subroutine": "Set", "args": "1,2"}]),
     [row(lib_func="Set", lib_para="value", lib_stb=body())], []),
    (body(steps=[{"no": 1, "subroutine": "Init", "args": "1"}]),
     [row(lib_func="Init", isinit=True, lib_stb=body())], []),
    (body(steps=[{"no": 1, "subroutine": "Cycle"}]),
     [row(lib_func="Cycle", lib_stb=body(steps=[{"no": 1, "subroutine": "Cycle"}]))], []),
    (body(), [], [row(const_name="BASE", const_value="nan")]),
    (body(), [], [row(const_name="BASE", const_value="2"),
                  row(const_name="BASE", const_value="3")]),
])
def test_invalid_conversion_rejected_before_materialisation(
        tmp_path, procedure, libraries, constants):
    target = tmp_path / "TC-1"
    with pytest.raises(ValueError):
        exporter.materialise_run_dir(target, row(steps=procedure), constants, libraries)
    assert not target.exists()


def test_list_arguments_and_defaults_are_validated(tmp_path):
    procedure = body(steps=[{"no": 1, "subroutine": "Set", "args": ["BASE+1"]}])
    library = row(lib_func="Set", lib_para="value=BASE",
                  lib_stb=body(steps=[{"no": 1, "inputs": ["value"]}]))
    manifest = exporter.materialise_run_dir(
        tmp_path / "TC-1", row(steps=procedure),
        [row(const_name="BASE", const_value="2")], [library])
    case = json.loads(manifest["testcase_json"].read_text(encoding="utf-8"))
    assert case["steps"][0]["actions"][0]["args"] == ["BASE+1"]
