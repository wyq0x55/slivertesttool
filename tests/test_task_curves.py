"""CSV fixtures model supported text formats, not proprietary Silver output."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import task_curves_service as curves

FIXTURES = Path(__file__).parent / "fixtures" / "curves"


def source(tmp_path, content=None, *, raw=None):
    root = tmp_path / "workspace"
    output = root / "log" / "TC-1" / "output.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(raw if raw is not None else (content or (FIXTURES / "numeric.csv").read_text()).encode())
    task = SimpleNamespace(id=1, task_key="T000001", project_id=1, run_count=1,
                           test_id="TC-1", workspace=str(root), report_path="", deleted_at=None)
    return task, output


def test_metadata_preserves_duplicates_unknown_units_and_types(tmp_path):
    task, _ = source(tmp_path)
    meta = curves.CsvResult(task).metadata()
    assert meta["suggested_time"] == "c0"
    assert meta["columns"][1]["name"] == meta["columns"][2]["name"] == "Speed"
    assert meta["columns"][1]["id"] != meta["columns"][2]["id"]
    assert meta["columns"][1]["label"] != meta["columns"][2]["label"]
    assert all(c["unit"] is None and c["type"] is None for c in meta["columns"])
    assert meta["columns"][4]["numeric_preview"] is False
    data = curves.CsvResult(task).data("c0", ["c1", "c2", "c3"])
    assert data["sampled"] is False
    assert len(data["points"]) == 6
    assert data["warnings"]["duplicate_time_rows"] == 1
    assert data["warnings"]["invalid_values"] == 1
    assert data["points"][4]["values"][1] is None


@pytest.mark.parametrize("encoding,delimiter", [("utf-8-sig", ","), ("utf-16", "\t"),
                                               ("gb18030", ";"), ("cp1252", "|")])
def test_bom_encoding_and_delimiters(tmp_path, encoding, delimiter):
    label = "温度" if encoding != "cp1252" else "Température"
    raw = f"Time{delimiter}{label}\r\n0{delimiter}1\r\n1{delimiter}2\r\n".encode(encoding)
    task, _ = source(tmp_path, raw=raw)
    csv = curves.CsvResult(task, encoding=encoding if encoding in ("gb18030", "cp1252") else None)
    assert csv.metadata()["columns"][1]["name"] == label
    assert csv.delimiter == delimiter
    assert csv.data("c0", ["c1"])["points"][1]["values"] == [2]


def test_explicit_separator_decimal_comma_and_time_selection(tmp_path):
    task, _ = source(tmp_path, raw=(FIXTURES / "semicolon.csv").read_bytes())
    csv = curves.CsvResult(task)
    assert csv.delimiter == ";"
    assert csv.metadata()["suggested_time"] is None
    assert csv.data("c0", ["c2"])["points"][0]["values"] == [76.5]


@pytest.mark.parametrize("raw,code", [(b"", "CSV_EMPTY"), (b"\xef\xbb\xbf", "CSV_EMPTY"),
                                    (b"Time,x\n", "CSV_NO_DATA"),
                                    (b"Time,x\na,b\n", "CSV_NO_DATA")])
def test_empty_and_no_numeric_time(tmp_path, raw, code):
    task, _ = source(tmp_path, raw=raw)
    with pytest.raises(curves.CurveError) as caught:
        curves.CsvResult(task).data("c0", ["c1"])
    assert caught.value.code == code


def test_ambiguous_legacy_encoding_requires_explicit_choice(tmp_path):
    task, _ = source(tmp_path, raw='Time,Température\n0,1\n'.encode('cp1252'))
    with pytest.raises(curves.CurveError) as caught:
        curves.CsvResult(task)
    assert caught.value.code == 'CSV_ENCODING'
    assert curves.CsvResult(task, encoding='cp1252').metadata()['columns'][1]['name'] == 'Température'


@pytest.mark.parametrize('text', ['Time,x\n0,"unclosed', ','.join(['x'] * 257) + '\n', 'Time,x\n' + '0,' + 'x' * 131073])
def test_malformed_quotes_width_and_line_limits(tmp_path, text):
    task, _ = source(tmp_path, text)
    with pytest.raises(curves.CurveError):
        curves.CsvResult(task).data('c0', ['c1'])


def test_row_and_read_budgets_are_enforced(tmp_path, monkeypatch):
    task, _ = source(tmp_path)
    csv = curves.CsvResult(task)
    monkeypatch.setattr(curves, 'MAX_ROWS', 3)
    with pytest.raises(curves.CurveError) as caught:
        csv.data('c0', ['c1'])
    assert caught.value.status == 413
    monkeypatch.setattr(curves, 'MAX_ROWS', 100)
    monkeypatch.setattr(curves, 'MAX_BYTES', 10)
    with pytest.raises(curves.CurveError) as caught:
        csv.data('c0', ['c1'])
    assert caught.value.status == 413


def test_multiline_raw_cells_keep_record_offsets(tmp_path):
    task, _ = source(tmp_path, 'Time,x,note\n0,1,"hello\nworld"\n1,2,next\n')
    csv = curves.CsvResult(task)
    assert csv.raw()['rows'][0]['cells'][2] == 'hello\nworld'
    assert csv.point('c0', ['c1'], 1)['raw'] == ['1', '2', 'next']


def test_non_monotonic_time_is_not_silently_sorted(tmp_path):
    task, _ = source(tmp_path, "Time,x\n0,1\n2,2\n1,3\n")
    csv = curves.CsvResult(task)
    with pytest.raises(curves.CurveError, match="not monotonic"):
        csv.data("c0", ["c1"])
    assert csv.raw()["rows"][2]["cells"] == ["1", "3"]


def test_bad_rows_nonfinite_values_and_text_stay_explicit(tmp_path):
    task, _ = source(tmp_path, "Time,x\n0,NaN\nbad,3\n1\n\n2,Infinity\n3,text\n4,5\n")
    data = curves.CsvResult(task).data("c0", ["c1"])
    assert data["warnings"] == {"blank_rows": 1, "malformed_rows": 1,
                                "invalid_time_rows": 1, "duplicate_time_rows": 0, "invalid_values": 3}
    assert [p["values"][0] for p in data["points"]] == [None, None, None, 5]
    json.dumps(data, allow_nan=False)


def test_bounded_shared_sampling_keeps_spikes_gaps_and_range_detail(tmp_path):
    lines = ["Time,a,b"]
    for i in range(10000):
        a = 99999 if i == 4444 else -99999 if i == 5555 else i % 7
        b = "bad" if i == 6000 else -77777 if i == 7777 else 1
        lines.append(f"{i / 100},{a},{b}")
    task, _ = source(tmp_path, "\n".join(lines))
    csv = curves.CsvResult(task)
    data = csv.data("c0", ["c1", "c2"], limit=100)
    assert data["sampled"] and len(data["points"]) <= 100
    rows = {p["row"] for p in data["points"]}
    assert {1, 10000, 4445, 5556, 6001, 7778} <= rows
    assert data["points"] == sorted(data["points"], key=lambda p: p["row"])
    detail = csv.data("c0", ["c1"], start=44.4, end=44.5, limit=100)
    assert not detail["sampled"]
    assert max(p["values"][0] for p in detail["points"]) == 99999
    point = csv.point("c0", ["c1"], 44.44001)
    assert point["row"] == 4445 and point["raw"][1] == "99999"


def test_cursor_duplicate_time_returns_original_first_record(tmp_path):
    task, _ = source(tmp_path)
    point = curves.CsvResult(task).point("c0", ["c2"], 0.2)
    assert point["duplicate_times"] == 2
    assert point["row"] == 3 and point["raw"][2] == "1200"


def test_raw_pagination_retains_original_text(tmp_path):
    task, _ = source(tmp_path)
    page = curves.CsvResult(task).raw(offset=3, count=2)
    assert page["has_more"] and len(page["rows"]) == 2
    assert page["rows"][1]["cells"][2] == "bad"


def test_limits_revision_and_invalid_options(tmp_path, monkeypatch):
    task, path = source(tmp_path)
    csv = curves.CsvResult(task)
    for time, cols in [("../../etc", ["c1"]), ("c0", ["c999"]), ("c0", ["c0"]), ("c0", ["c1", "c1"])]:
        with pytest.raises(curves.CurveError):
            csv.data(time, cols)
    with pytest.raises(curves.CurveError):
        csv.data("c0", ["c1"], limit=99999)
    with pytest.raises(curves.CurveError):
        csv.data("c0", ["c1"], start=2, end=1)
    path.write_text("Time,x\n0,2\n")
    with pytest.raises(curves.CurveError) as caught:
        curves.CsvResult(task, expected_revision=csv.revision)
    assert caught.value.status == 409
    monkeypatch.setattr(curves, "MAX_BYTES", 8)
    with pytest.raises(curves.CurveError):
        curves.CsvResult(task).data("c0", ["c1"])


def test_fixed_artifact_ignores_foreign_report_path_and_rejects_symlink(tmp_path):
    task, path = source(tmp_path)
    foreign = tmp_path / "foreign.csv"
    foreign.write_text("Time,secret\n0,123\n")
    task.report_path = str(foreign)
    assert curves.CsvResult(task).path == path
    path.unlink()
    path.symlink_to(foreign)
    with pytest.raises(curves.CurveError) as caught:
        curves.CsvResult(task)
    assert caught.value.status == 404
    path.unlink()
    task.test_id = "../other"
    with pytest.raises(curves.CurveError):
        curves.CsvResult(task)


@pytest.fixture
def api_env(app_ctx, client, tmp_path):
    from app.extensions import db
    from app.models import LMUser, Project, ProjectMember, Task
    with app_ctx.app_context():
        owner = LMUser(username="csv-owner", display_name="Owner", password_hash="x")
        reader = LMUser(username="csv-reader", display_name="Reader", password_hash="x")
        outsider = LMUser(username="csv-outsider", display_name="Outsider", password_hash="x")
        db.session.add_all([owner, reader, outsider]); db.session.flush()
        project = Project(code="CSV", name="CSV", owner_id=owner.id)
        other = Project(code="CSVOTHER", name="Other", owner_id=outsider.id)
        db.session.add_all([project, other]); db.session.flush()
        db.session.add(ProjectMember(project_id=project.id, user_id=reader.id, role="reader"))
        task_data, path = source(tmp_path)
        task = Task(task_key="T000010", project_id=project.id, test_id="TC-1", workspace=task_data.workspace, status="passed")
        foreign = Task(task_key="T000011", project_id=other.id, test_id="TC-2", workspace=str(tmp_path / "foreign"), status="passed")
        db.session.add_all([task, foreign]); db.session.commit()
        values = dict(project=project.id, other=other.id, task_id=task.id, reader=reader.id,
                      outsider=outsider.id, path=path)
    return values


def login(client, user_id):
    with client.session_transaction() as session:
        session["lm_user_id"] = user_id


@pytest.mark.parametrize("suffix", ["", "/data?time=c0&columns=c1", "/point?time=c0&columns=c1&at=0.2", "/raw", "/download"])
def test_api_permissions_for_every_curve_entry(client, api_env, suffix):
    env = api_env
    base = f'/api/v1/projects/{env["project"]}/tasks/T000010/curves'
    assert client.get(base + suffix).status_code == 401
    login(client, env["outsider"])
    assert client.get(base + suffix).status_code in (403, 404)
    login(client, env["reader"])
    assert client.get(base + suffix).status_code == 200
    assert client.get(f'/api/v1/projects/{env["project"]}/tasks/T000011/curves' + suffix).status_code == 404
    assert client.get(f'/api/v1/projects/{env["other"]}/tasks/T000011/curves' + suffix).status_code in (403, 404)


def test_api_no_artifact_invalid_params_and_download(client, api_env):
    login(client, api_env["reader"])
    base = f'/api/v1/projects/{api_env["project"]}/tasks/T000010/curves'
    for query in ('time=c0&columns=c1&start=nan', 'time=c0&columns=c1&limit=abc', 'time=c0&columns=c1&limit=999999', 'time=c0&columns=../../output.csv'):
        assert client.get(base + '/data?' + query).status_code == 400
    response = client.get(base + '/download')
    assert response.data == api_env['path'].read_bytes()
    assert 'attachment' in response.headers['Content-Disposition']
    assert response.headers['Cache-Control'] == 'no-store'
    api_env['path'].write_bytes(b'')
    assert client.get(base).status_code == 422
    assert client.get(base + '/download').status_code == 200
    assert client.get(base + '/download').data == b''
    api_env['path'].unlink()
    assert client.get(base).status_code == 404


def test_sealed_attempt_identity_digest_and_no_legacy_fallback(client, api_env, tmp_path):
    from app.extensions import db
    from app.models import Task
    from app.services import run_evidence_service as evidence
    login(client, api_env['reader'])
    base = f'/api/v1/projects/{api_env["project"]}/tasks/T000010/curves'
    with client.application.app_context():
        task = db.session.get(Task, api_env['task_id'])
        model = tmp_path / 'model.sil'; model.write_text('fixture model; never executed')
        task.sil_relpath = str(model)
        input_dir = Path(task.workspace) / 'input'; input_dir.mkdir()
        (input_dir / 'judge.py').write_text('# fixture')
        root = evidence.pin_attempt(task, input_dir)
        results = root / 'results'; results.mkdir()
        output = results / 'output.csv'; output.write_bytes((FIXTURES / 'numeric.csv').read_bytes())
        evidence.seal_attempt(task, status='passed', verdict='PASS', message='fixture', evidence_kind='mock', runner_backend='mock')
        db.session.commit()
        assert client.get(base).status_code == 200
        # CSV preview validates only its selected result, not unrelated mutable inputs.
        output.write_text('Time,foreign\n0,987\n')
        assert client.get(base).status_code == 404
        output.unlink()
        assert client.get(base).status_code == 404
