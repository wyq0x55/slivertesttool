import io
import json
import subprocess
import sys
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from app.runners.judge_bundler import bundle_judge, is_bundled, unresolved_local_imports
from app.services.lanmatrix.legacy_judge_export import LegacyExportError, build_legacy_zip


CASE_ID = "ID;;CASE-001"
ROOT = "01_Spec_Report/DSP/CSVLESS"
CASE = f"{ROOT}/TestCase/{CASE_ID}"


def _row(case_id="", **fields):
    return SimpleNamespace(case_id=case_id, get_field=fields.get)


def _body(*steps, inputs=(), expected=()):
    return {"steps": list(steps), "input_signals": list(inputs),
            "expected_signals": list(expected)}


def _lib(name, body, *, init=False, params=""):
    return _row(lib_func=name, isinit=init, lib_para=params, lib_stb=body)


def test_legacy_archive_executes_and_bundles(tmp_path):
    case = _row(CASE_ID, steps=json.dumps(_body(
        {"no": 1, "subroutine": "SystemInitialize", "args": "2"},
        {"no": 2, "operation": "Set 'input'\nthen check", "subroutine": "RunSequence",
         "args": "LIMIT + 2"},
        {"no": 3, "inputs": ["点1 X((LIMIT + 1)*2)"],
         "expecteds": ["range[5,8)", "≧2", "1, 2, 3", "0 || 6", "* → 6", "T.B.D",
                       ">$reference$", "OPTIONS", "(0,6]", "[5, )"], "timing": "即座"},
        {"no": 4, "expecteds": ["6"], "timing": "3ms WATCH"},
        inputs=[["点群", "input"]],
        expected=[["範囲", "input"], ["masked", "bit(1, 2, input)"],
                  ["list", "list"], ["or", "input"], ["transition", "input"],
                  ["pending", "T.B.D"], ["relative", "input"], ["choices", "list"],
                  ["range2", "input"], ["range3", "input"]],
    ), ensure_ascii=False))
    init = _lib("SystemInitialize", _body({"no": 1, "inputs": ["value"]},
                                        inputs=[["reference", "reference"]]),
                init=True, params=":value=0")
    routine = _lib("RunSequence", _body(
        {"no": 1, "subroutine": "SetList", "args": "value / 2"},
        {"no": 2, "subroutine": "Wait", "args": "2"},
    ), params="value")
    child = _lib("SetList", _body({"no": 1, "inputs": ["value"], "expecteds": ["2"],
                                  "timing": "1ms以内"},
                                 inputs=[["list", "list"]], expected=[["list", "list"]]),
                 params="value")
    constants = [_row(const_name="LIMIT", const_value="2"),
                 _row(const_name="OPTIONS", const_value="MagicList([2, 3])")]
    with ZipFile(build_legacy_zip("DSP", [case], constants, [init, routine, child])) as archive:
        assert archive.read(f"{CASE}/output.txt") == b"input\r\nlist\r\nreference\r\n"
        assert archive.read(f"{CASE}/judge.py").startswith(b"\xef\xbb\xbf")
        assert f"{ROOT}/Lib/SetList.py" in archive.namelist()
        archive.extractall(tmp_path)

    judge = tmp_path / CASE / "judge.py"
    assert unresolved_local_imports(judge) == []
    bundled = bundle_judge(judge)
    assert is_bundled(bundled)
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    bundled_judge = isolated / "judge.py"
    bundled_judge.write_text(bundled, encoding="utf-8")
    log = tmp_path / "jdgrslt.log"
    completed = subprocess.run([sys.executable, "-c", """
import runpy
import sys
from types import ModuleType, SimpleNamespace

values = {'currentTime': 0.0, 'modelStepSize': 0.001}


class Variable:
    def __init__(self, name):
        self.name = name
        self.duration = 0

    @property
    def Value(self):
        return values.get(self.name, 0)

    @Value.setter
    def Value(self, value):
        values[self.name] = value


sys.modules['synopsys'] = ModuleType('synopsys')
sys.modules['synopsys.silver'] = SimpleNamespace(Variable=Variable, DLL_OK=1)
sys.modules['synopsys.util'] = SimpleNamespace(scheduler=SimpleNamespace())
sys.argv = sys.argv[1:]
judge = runpy.run_path(sys.argv[0])
assert judge['pre_init'](0) == 1
assert values['reference'] == 2
clock = judge['MainGenerator']()
for tick in clock:
    assert tick is None
    values['currentTime'] = round(values['currentTime'] + 0.001, 3)
assert values['input'] == 6
assert values['list'] == 2
assert judge['pre_cleanup'](values['currentTime']) == 1
""", str(bundled_judge), str(log)], capture_output=True, text=True, timeout=20)
    assert completed.returncode == 0, completed.stderr
    result = log.read_text(encoding="utf-8")
    assert "Step.4 is passed at 0.004s." in result
    assert "Monitoring target: 範囲 ( input )" in result
    assert "Observed Value:0x6 ( 6 )" in result
    assert "確認タイミング：即座" in result
    assert "▼" in result
    assert "All steps are verified. Test is Passed. The test has T.B.D. judge value." in result


@pytest.mark.parametrize("case_id, expression, message", [
    ("../CASE", "1", "Invalid export directory"),
    ("CASE", "UNKNOWN", "Undefined constant"),
    ("CASE", "(open('x'))", "Unsupported expression"),
])
def test_invalid_legacy_inputs_report_the_case(case_id, expression, message):
    case = _row(case_id, steps=_body({"no": 1, "inputs": [expression]},
                                    inputs=[["input", "input"]]))
    with pytest.raises(LegacyExportError, match=message) as error:
        build_legacy_zip("DSP", [case], [], [])
    assert str(error.value).startswith(case_id + ":")


def test_export_routes_filter_and_enforce_permissions(client, app_ctx):
    from app.extensions import db
    from app.models import LMUser, Project, ProjectMember
    from app.models import TestItemRow as MatrixRow

    with app_ctx.app_context():
        owner = LMUser(username="export-owner", display_name="Export owner", password_hash="unused")
        reader = LMUser(username="export-reader", display_name="Export reader", password_hash="unused")
        outsider = LMUser(username="export-outsider", display_name="Outsider", password_hash="unused")
        db.session.add_all([owner, reader, outsider])
        db.session.flush()
        project = Project(code="DSP", name="Silver export", owner_id=owner.id, created_by=owner.id)
        db.session.add(project)
        db.session.flush()
        project_id, reader_id, outsider_id = project.id, reader.id, outsider.id
        db.session.add(ProjectMember(project_id=project_id, user_id=reader_id, role="reader"))
        for test_id, body, status in [
            (CASE_ID, _body({"no": 1}), ""),
            ("CASE-002", _body({"no": 1}), "不要"),
            ("CASE-003", _body(), "未作成"),
        ]:
            db.session.add(MatrixRow(project_id=project_id, case_id=test_id,
                                     custom_values={"steps": body, "item_created": status}))
        db.session.commit()

    legacy = f"/api/v1/projects/{project_id}/legacy-judge/export"
    current = f"/api/v1/projects/{project_id}/silver-json/export"
    assert client.get(legacy).status_code == 401
    with client.session_transaction() as session:
        session["lm_user_id"] = outsider_id
    assert client.get(legacy).status_code in (403, 404)
    with client.session_transaction() as session:
        session["lm_user_id"] = reader_id
    response = client.get(legacy)
    assert response.status_code == 200
    assert response.mimetype == "application/zip"
    with ZipFile(io.BytesIO(response.data)) as archive:
        assert [path for path in archive.namelist() if path.endswith("/judge.py")] == [f"{CASE}/judge.py"]
    response = client.get(legacy, query_string={"test_ids": "CASE-002"})
    assert response.status_code == 200
    with ZipFile(io.BytesIO(response.data)) as archive:
        assert f"{ROOT}/TestCase/CASE-002/judge.py" in archive.namelist()
    assert client.get(legacy, query_string={"test_ids": "MISSING"}).status_code == 400
    response = client.get(current)
    assert response.status_code == 200
    with ZipFile(io.BytesIO(response.data)) as archive:
        assert f"{CASE_ID}/silver_json_runner.py" in archive.namelist()
        assert f"{CASE_ID}/testcase_ID_CASE-001.json" in archive.namelist()
