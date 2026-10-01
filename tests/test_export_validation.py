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


@pytest.mark.parametrize("number", [1.0, 2.0])
def test_export_accepts_integral_step_numbers_from_imported_rows(tmp_path, number):
    from app.services.run_validation_service import validate_documents

    manifest = exporter.materialise_run_dir(
        tmp_path / "TC-1", row(steps=body(steps=[{"no": number, "inputs": ["BASE"]}])),
        [row(const_name="BASE", const_value="2")], [])
    testcase = json.loads(manifest["testcase_json"].read_text(encoding="utf-8"))
    constants = json.loads(manifest["constants_json"].read_text(encoding="utf-8"))
    library = json.loads(manifest["lib_json"].read_text(encoding="utf-8"))

    steps = validate_documents(testcase, constants, library)

    assert steps[0].no == number
    assert steps[0].inputs[0].value == 2


@pytest.mark.parametrize("initialisation", [False, True])
def test_export_accepts_integral_step_numbers_in_existing_libraries(tmp_path, initialisation):
    from app.services.run_validation_service import validate_documents

    procedure = body(steps=[{"no": 1.0, "subroutine": "Set"}])
    existing_library = row(lib_func="Set", isinit=initialisation,
                           lib_stb=body(steps=[{"no": 1.0, "inputs": ["BASE"]}]))
    manifest = exporter.materialise_run_dir(
        tmp_path / "TC-1", row(steps=procedure),
        [row(const_name="BASE", const_value="2")], [existing_library])
    testcase = json.loads(manifest["testcase_json"].read_text(encoding="utf-8"))
    constants = json.loads(manifest["constants_json"].read_text(encoding="utf-8"))
    library = json.loads(manifest["lib_json"].read_text(encoding="utf-8"))

    assert validate_documents(testcase, constants, library)[0].no == 1
    assert validate_documents(library["subroutines"]["Set"], constants, library)[0].inputs[0].value == 2


@pytest.mark.parametrize("numbers", [
    [True], [False], [0.0], [-1.0], [0.5], [1.5], ["1"], [None],
    [float("inf")], [float("nan")], [1, 1.0], [2.0, 2.0],
])
def test_export_rejects_invalid_or_duplicate_step_numbers(tmp_path, numbers):
    procedure = body(steps=[{"no": number, "inputs": ["BASE"]} for number in numbers])

    with pytest.raises(ValueError):
        exporter.materialise_run_dir(
            tmp_path / "TC-1", row(steps=procedure),
            [row(const_name="BASE", const_value="2")], [])

    assert not (tmp_path / "TC-1").exists()


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


@pytest.mark.parametrize("expression,operation", [("2**1000000000", "pow"),
                                                   ("1<<1000000000", "lshift"),
                                                   ("'X'*1000000000", "mul")])
def test_validation_rejects_unbounded_expression_before_native_operation(monkeypatch, expression, operation):
    from app.services import run_validation_service as validation
    calls = []

    def forbidden(left, right):
        calls.append((left, right))
        raise ValueError("unsafe native operation reached")

    monkeypatch.setattr(validation.operator, operation, forbidden)
    validation._parser.cache_clear()
    try:
        with pytest.raises(ValueError):
            validation.validate_documents({"steps": [{"no": 1, "inputs": [{"var": "IN", "value": expression}]}]},
                                          {"constants": {}}, {"subroutines": {}})
        assert calls == []
    finally:
        validation._parser.cache_clear()
