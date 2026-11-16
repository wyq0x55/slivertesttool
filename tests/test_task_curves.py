"""Variable curves: CsvWriter output.csv index, judge context, API and views.

The CSV text below is synthetic but follows the verified CsvWriter layout
(quoted header, first column ``time`` in seconds, 1 ms records, integer cells).
No proprietary Silver output is stored in the repository.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import curve_context_service as context
from app.services import task_curves_service as curves


def csvwriter(rows, names=("Rte_IN_PP_Door_u1_Door", "Rte_OUT_PP_Mode_u1_Mode", "stl_Timer[0].u2_msec", "u1_const")):
    lines = [",".join(f'"{n}"' for n in ("time", *names))]
    for i, values in enumerate(rows):
        lines.append(",".join([f"{i / 1000:g}", *map(str, values)]))
    return "\r\n".join(lines) + "\r\n"


def sample():
    rows = []
    for i in range(3000):
        door = 1 if 500 <= i < 2000 else 0
        mode = 0 if i < 510 else 2 if i < 2010 else 1
        rows.append((door, mode, i * 5 % 65536, 7))
    return csvwriter(rows)


JDG = """INFO:root:Test case ID.ID;;TC-1 is started!
INFO:root:-------------------pre_init-------------------
INFO:root:前提条件の確認
INFO:root:Step.1 is passed at 0.0s.
INFO:root:-------------------Step2-------------------
INFO:root:トリガ入力
INFO:root:●
INFO:root:    Monitoring target: ドア状態 ( Rte_IN_PP_Door_u1_Door )
INFO:root:    Expected Value:ドア開 ( (exp==U1G_DOOR_OPEN(1)) )
INFO:root:    Observed Value:0x1 ( 1.0 )
INFO:root:    確認タイミング：任意
INFO:root:Step.2 is passed at 0.5s.
INFO:root:-------------------Step3-------------------
INFO:root:結果確認
INFO:root:▲
INFO:root:    Monitoring target: モード ( Rte_OUT_PP_Mode_u1_Mode )
INFO:root:    Expected Value:サーチ ( (exp==U1G_MODE_SEARCH(3)) )
INFO:root:    Observed Value:0x2 ( 2.0 )
INFO:root:    確認タイミング：1.0s以内
INFO:root:Step.3 is failed at 1.5s.
INFO:root:Test is failed in Step3!!!!
"""


def legacy_task(tmp_path, text=None, *, judge=JDG):
    root = tmp_path / "workspace"
    log = root / "log" / "TC-1"
    log.mkdir(parents=True, exist_ok=True)
    (log / "output.csv").write_text(text if text is not None else sample(), newline="")
    if judge is not None:
        (log / "jdgrslt.log").write_text(judge, encoding="utf-8")
    task = SimpleNamespace(id=1, task_key="T000001", project_id=1, run_count=1, status="failed",
                           test_id="TC-1", workspace=str(root), report_path="", deleted_at=None)
    return task, log / "output.csv"


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    monkeypatch.setattr(curves, "cache_dir", lambda: tmp_path / "cache")
    curves._MEMORY.clear()


def idx_for(tmp_path, text=None):
    task, _ = legacy_task(tmp_path, text)
    source = curves.resolve(task)
    return source, curves.index(source)


# --------------------------------------------------------------------------- #
# Index
# --------------------------------------------------------------------------- #
def test_change_points_kinds_and_metadata(tmp_path):
    _, idx = idx_for(tmp_path)
    meta = idx.metadata(32)
    kinds = {c["name"]: (c["kind"], c["changes"]) for c in meta["columns"]}
    assert kinds["Rte_IN_PP_Door_u1_Door"] == ("discrete", 2)
    assert kinds["Rte_OUT_PP_Mode_u1_Mode"] == ("discrete", 2)
    assert kinds["stl_Timer[0].u2_msec"][0] == "analog"
    assert kinds["u1_const"] == ("constant", 0)
    assert meta["rows"] == 3000 and meta["domain"] == [0, 2.999] and meta["sample_interval"] == pytest.approx(0.001)
    door = meta["columns"][0]
    assert door["values"] == [0, 1] and door["initial"] == 0


def test_series_is_exact_until_the_limit_then_envelope(tmp_path):
    _, idx = idx_for(tmp_path)
    data = idx.series(["c1", "c2", "c3"], start=0.4, end=2.5, width=400, point_limit=100)
    door, mode, timer = data["series"]
    assert door["mode"] == "exact"
    # value holding at the range start, then each change at its own record
    assert door["points"] == [[0.4, 0, 1], [0.5, 1, 501], [2.0, 0, 2001]]
    assert mode["points"][1:] == [[0.51, 2, 511], [2.01, 1, 2011]]
    assert timer["mode"] == "envelope" and len(timer["buckets"]) <= 400
    first = timer["buckets"][0]
    assert first[3] <= first[4]  # min <= max per pixel
    json.dumps(data, allow_nan=False)


def test_values_edges_events_and_divergence(tmp_path):
    _, idx = idx_for(tmp_path)
    assert idx.values_at(["c1", "c2"], 0.5004, 32)["values"] == {"c1": 1, "c2": 0}
    assert idx.edge("c1", 0.1, 1)["t"] == 0.5
    assert idx.edge("c1", 0.5, 1)["t"] == 2.0
    assert idx.edge("c1", 2.0, -1)["t"] == 0.5
    assert idx.edge("c1", 0.5, -1)["t"] is None
    events = idx.events(["c1", "c2"])
    assert events["total"] == 4
    assert [(e["t"], e["id"], e["from"], e["to"]) for e in events["events"]] == [
        (0.5, "c1", 0, 1), (0.51, "c2", 0, 2), (2.0, "c1", 1, 0), (2.01, "c2", 2, 1)]
    assert idx.events(["c1"], start=1, end=3)["total"] == 1
    changed = sample().replace("\r\n1.5,1,2,", "\r\n1.5,1,3,")
    _, other = idx_for(tmp_path / "b", changed)
    result = idx.divergence(other)
    assert result["first"] == {"id": "c2", "other_id": "c2", "name": "Rte_OUT_PP_Mode_u1_Mode", "t": 1.5}
    assert idx.divergence(idx)["first"] is None


def test_cache_round_trip_and_revision_binding(tmp_path):
    source, idx = idx_for(tmp_path)
    cached = tmp_path / "cache" / f"{source.revision}.idx"
    assert cached.is_file()
    loaded = curves.Index.load(cached, source.revision)
    assert loaded.metadata(32) == idx.metadata(32)
    assert curves.Index.load(cached, "other-revision") is None


@pytest.mark.parametrize("text,code", [
    ("", "CSV_EMPTY"),
    ('"t","x"\r\n0,1\r\n', "CSV_FORMAT"),
    ('"time","x"\r\n', "CSV_NO_DATA"),
    ('"time","x"\r\n0,1\r\n0.001\r\n', "CSV_FORMAT"),
    ('"time","x"\r\n0.002,1\r\n0.001,1\r\n', "CSV_FORMAT"),
    ('"time","x"\r\nbad,1\r\n', "CSV_FORMAT"),
])
def test_not_csvwriter_output_fails_explicitly(tmp_path, text, code):
    task, _ = legacy_task(tmp_path, text)
    with pytest.raises(curves.CurveError) as caught:
        curves.index(curves.resolve(task))
    assert caught.value.code == code


@pytest.mark.parametrize("tail", ["0.001", "0.001,", "0.002,"])
def test_archived_short_unterminated_tail_is_reported_and_shared(tmp_path, tail):
    text = '"time","x","y"\r\n0,1,2\r\n0.001,3,4\r\n' + tail
    source, idx = idx_for(tmp_path, text)
    original = source.path.read_bytes()
    expected = {"record": 4, "fields": len(tail.split(",")), "expected_fields": 3}
    assert idx.metadata(32)["truncation"] == expected
    assert list(idx.times) == [0, 0.001]
    assert idx.series(["c2"])["series"][0]["points"] == [[0, 2, 1], [0.001, 4, 2]]
    assert curves.raw(source)["rows"] == [
        {"row": 1, "cells": ["0", "1", "2"]},
        {"row": 2, "cells": ["0.001", "3", "4"]},
    ]
    assert "".join(curves.export_csv(source, idx, ["c1", "c2"])) == (
        "time,x,y\r\n0,1,2\r\n0.001,3,4\r\n")
    loaded = curves.Index.load(tmp_path / "cache" / f"{source.revision}.idx", source.revision)
    assert loaded.metadata(32) == idx.metadata(32)
    assert source.path.read_bytes() == original


@pytest.mark.parametrize("ending", ["", "\n", "\r\n"])
def test_complete_final_record_remains_valid_without_newline(tmp_path, ending):
    source, idx = idx_for(tmp_path, '"time","x"\r\n0,1\r\n0.001,2' + ending)
    assert list(idx.times) == [0, 0.001]
    assert idx.metadata(32)["truncation"] is None
    assert curves.raw(source)["rows"][-1] == {"row": 2, "cells": ["0.001", "2"]}
    assert "".join(curves.export_csv(source, idx, ["c1"])) == "time,x\r\n0,1\r\n0.001,2\r\n"


@pytest.mark.parametrize("data", [
    b'"time","x","y"\r\n0,1,2\r\n0.001,3\r\n',
    b'"time","x","y"\r\n0,1,2\r\n0.001,3\r\n0.002,4,5',
    b'"time","x","y"\r\n0,1,2\r\n0.001,3\n ',
    b'"time","x","y"\r\n0,1,2\r\n0.001,3,4,5',
    b'"time","x","y"\r\n0,1,2\r\n0.001,3,4,5\r\n',
    b'"time","x","y"\r\n0,1,2\r\nbad,3',
    b'"time","x","y"\r\n0,1,2\r\nnan,3',
    b'"time","x","y"\r\n0,1,2\r\ninf,3',
    b'"time","x","y"\r\n0,1,2\r\n,3',
    b'"time","x","y"\r\n0,1,2\r\n-0.001,3',
    b'"time","x","y"\r\n0,1,2\r\n0.001,\xff',
    b'"time","x","y"\r\n0,1,2\r\n0.001,\xff,3\r\n0.002,4,5',
    b'"time","x","y"\r\n0,1,2\r\n0.001,"3',
    b'"time","x","y"\r\n0,1',
])
def test_tail_recovery_keeps_corruption_strict_for_all_readers(tmp_path, data):
    task, path = legacy_task(tmp_path, '"time","x","y"\r\n0,1,2\r\n0.001,3,4\r\n')
    source = curves.resolve(task)
    idx = curves.Index.parse(source, len(data) + 1024)
    path.write_bytes(data)
    for read in (
        lambda: curves.Index.parse(source, len(data)),
        lambda: curves.raw(source),
        lambda: list(curves.export_csv(source, idx, ["c1", "c2"])),
    ):
        with pytest.raises(curves.CurveError) as caught:
            read()
        assert caught.value.code == "CSV_FORMAT"


def test_non_numeric_cells_are_gaps_not_zero(tmp_path):
    _, idx = idx_for(tmp_path, '"time","x"\r\n0,1\r\n0.001,nan\r\n0.002,1\r\n')
    assert idx.series(["c1"])["series"][0]["points"] == [[0, 1, 1], [0.001, None, 2], [0.002, 1, 3]]


def test_size_limit_and_attempt_binding(tmp_path, monkeypatch):
    task, _ = legacy_task(tmp_path)
    monkeypatch.setattr(curves, "limits", lambda: curves.Limits(10, 5000, 32, 1 << 20))
    with pytest.raises(curves.CurveError) as caught:
        curves.resolve(task)
    assert caught.value.status == 413
    monkeypatch.undo()
    with pytest.raises(curves.CurveError):
        curves.resolve(task, 2)  # a legacy task only has its latest result


def test_live_file_ignores_the_partial_last_record(tmp_path, monkeypatch):
    pool = tmp_path / "pool"
    live = pool / "inst_1" / "run_T000001_1" / ".results"
    live.mkdir(parents=True)
    (live / "output.csv").write_text('"time","x"\r\n0,1\r\n0.001,2\r\n0.00', newline="")
    from app.config import Config
    monkeypatch.setattr(Config, "POOL_DIR", pool)
    task, _ = legacy_task(tmp_path)
    task.status = "running"
    source = curves.resolve(task, live=True)
    idx = curves.index(source)
    assert idx.live and len(idx.times) == 2
    task.status = "passed"
    with pytest.raises(curves.CurveError):
        curves.resolve(task, live=True)


# --------------------------------------------------------------------------- #
# Judge timeline, dictionary, report snapshot
# --------------------------------------------------------------------------- #
def test_timeline_and_judge_dictionary(tmp_path):
    task, _ = legacy_task(tmp_path)
    judge = context.timeline(task)
    assert judge["verdict"] == "FAIL"
    assert [(s["step"], s["result"], s["start"], s["end"]) for s in judge["steps"]] == [
        (1, "PASS", 0.0, 0.0), (2, "PASS", 0.0, 0.5), (3, "FAIL", 0.5, 1.5)]
    bad = judge["steps"][2]["checks"][0]
    assert (bad["var"], bad["name"], bad["observed_value"], bad["at"]) == ("Rte_OUT_PP_Mode_u1_Mode", "モード", 2, 1.5)
    assert bad["expected_values"] == [{"name": "U1G_MODE_SEARCH", "value": 3}]
    assert judge["first_failure"] == {"step": 3, "start": 0.5, "end": 1.5, "var": "Rte_OUT_PP_Mode_u1_Mode", "at": 1.5}
    signals = context.dictionary(task, judge=judge)["signals"]
    assert signals["Rte_IN_PP_Door_u1_Door"] == {"name": "ドア状態", "roles": ["check"],
                                                 "labels": {"1": ["U1G_DOOR_OPEN", "ドア開"]}}


def test_missing_judge_log_is_reported_not_invented(tmp_path):
    task, _ = legacy_task(tmp_path, judge=None)
    assert context.timeline(task) == {"available": False, "verdict": None, "steps": [], "first_failure": None}


def test_pinned_procedure_supplies_input_names_and_constant_labels(tmp_path, monkeypatch):
    task, _ = legacy_task(tmp_path, judge=None)
    pinned = {"approved_inputs": {
        "test": {"input_signals": [["ドア", "Rte_IN_PP_Door_u1_Door"]],
                 "expected_signals": [["モード", "Rte_OUT_PP_Mode_u1_Mode"]],
                 "steps": [{"inputs": ["ドア開(U1G_DOOR_OPEN)"], "expecteds": ["-"]},
                           {"inputs": ["U1G_DOOR_CLOSE"], "expecteds": ["サーチ(U1G_MODE_SEARCH)"]}]},
        "libraries": {},
        "constants": {"constants": {"U1G_DOOR_OPEN": {"value": 1, "name_ja": "開"},
                                    "U1G_DOOR_CLOSE": {"value": 0, "name_ja": ""},
                                    "U1G_MODE_SEARCH": {"value": 3, "name_ja": "サーチ"}}}}}
    monkeypatch.setattr(context.evidence, "read_manifest", lambda *_a, **_k: pinned)
    d = context.dictionary(task)
    assert d["source"] == "pinned"
    assert d["signals"]["Rte_IN_PP_Door_u1_Door"] == {
        "name": "ドア", "roles": ["input"], "labels": {"1": ["U1G_DOOR_OPEN", "開"], "0": ["U1G_DOOR_CLOSE"]}}
    assert d["signals"]["Rte_OUT_PP_Mode_u1_Mode"]["labels"] == {"3": ["U1G_MODE_SEARCH", "サーチ"]}


def test_failure_snapshot_svg_draws_failed_window(tmp_path):
    from app.services.curve_snapshot import failure_svg
    task, _ = legacy_task(tmp_path)
    svg = failure_svg(task)
    assert svg.startswith("<svg") and "Step3 FAIL" in svg and "モード" in svg
    assert "#e5484d" in svg  # the failed check marker
    task_ok, _ = legacy_task(tmp_path / "ok", judge=JDG.replace("▲", "●").replace("failed", "passed"))
    assert failure_svg(task_ok) is None


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@pytest.fixture
def api_env(app_ctx, client, tmp_path):
    from app.extensions import db
    from app.models import LMUser, Project, ProjectMember, Task
    with app_ctx.app_context():
        owner = LMUser(username="csv-owner", display_name="Owner", password_hash="x")
        reader = LMUser(username="csv-reader", display_name="Reader", password_hash="x")
        other_reader = LMUser(username="csv-reader2", display_name="Reader2", password_hash="x")
        outsider = LMUser(username="csv-outsider", display_name="Outsider", password_hash="x")
        db.session.add_all([owner, reader, other_reader, outsider]); db.session.flush()
        project = Project(code="CSV", name="CSV", owner_id=owner.id)
        other = Project(code="CSVOTHER", name="Other", owner_id=outsider.id)
        db.session.add_all([project, other]); db.session.flush()
        db.session.add_all([ProjectMember(project_id=project.id, user_id=reader.id, role="reader"),
                            ProjectMember(project_id=project.id, user_id=other_reader.id, role="reader")])
        data, path = legacy_task(tmp_path / "a")
        data_b, _ = legacy_task(tmp_path / "b", sample().replace("\r\n1.5,1,2,", "\r\n1.5,1,3,"))
        task = Task(task_key="T000010", project_id=project.id, test_id="TC-1", workspace=data.workspace, status="failed")
        second = Task(task_key="T000012", project_id=project.id, test_id="TC-1B", workspace=data_b.workspace, status="passed")
        Path(data_b.workspace, "log", "TC-1").rename(Path(data_b.workspace, "log", "TC-1B"))
        foreign = Task(task_key="T000011", project_id=other.id, test_id="TC-2", workspace=str(tmp_path / "foreign"), status="passed")
        db.session.add_all([task, second, foreign]); db.session.commit()
        values = dict(project=project.id, other=other.id, task_id=task.id, reader=reader.id,
                      reader2=other_reader.id, outsider=outsider.id, path=path)
    return values


def login(client, user_id):
    with client.session_transaction() as session:
        session["lm_user_id"] = user_id
        session["csrf_token"] = "t"


CURVE_ENTRIES = ["", "/series?columns=c1", "/values?columns=c1&at=0.2", "/edge?column=c1&at=0&dir=1",
                 "/events", "/compare?other=T000012", "/raw", "/download", "/export?columns=c1,c2&start=0.4&end=0.6"]


@pytest.mark.parametrize("suffix", CURVE_ENTRIES)
def test_api_permissions_for_every_curve_entry(client, api_env, suffix):
    env = api_env
    base = f'/api/v1/projects/{env["project"]}/tasks/T000010/curves'
    assert client.get(base + suffix).status_code == 401
    login(client, env["outsider"])
    assert client.get(base + suffix).status_code in (403, 404)
    login(client, env["reader"])
    assert client.get(base + suffix).status_code == 200
    assert client.get(f'/api/v1/projects/{env["project"]}/tasks/T000011/curves' + suffix).status_code == 404


def test_api_context_compare_export_and_bad_params(client, api_env):
    login(client, api_env["reader"])
    base = f'/api/v1/projects/{api_env["project"]}/tasks/T000010/curves'
    data = client.get(base).get_json()["data"]
    assert data["judge"]["first_failure"]["step"] == 3 and data["attempts"] == [1]
    assert data["dictionary"]["signals"]["Rte_OUT_PP_Mode_u1_Mode"]["name"] == "モード"
    assert data["can_download"] is True and data["limits"]["lanes"] == 32
    compare = client.get(base + "/compare?other=T000012").get_json()["data"]
    assert compare["first"]["t"] == 1.5 and compare["other"]["task_key"] == "T000012"
    export = client.get(base + "/export?columns=c1,c2&start=0.499&end=0.501")
    assert export.data.decode() == ("time,Rte_IN_PP_Door_u1_Door,Rte_OUT_PP_Mode_u1_Mode\r\n"
                                    "0.499,0,0\r\n0.5,1,0\r\n0.501,1,0\r\n")
    assert "attachment" in export.headers["Content-Disposition"]
    for query in ("/series?columns=c1&start=nan", "/series?columns=c999", "/series?columns=../x",
                  "/series?columns=c1&width=5", "/values?columns=c1", "/edge?column=c1&at=0&dir=2",
                  "/series?run=2&columns=c1", f"/series?columns={','.join(['c1'] * 2)}"):
        assert client.get(base + query).status_code == 400, query
    assert client.get(base + "/series?columns=c1&revision=stale").status_code == 409
    assert client.get(base + "/compare").status_code == 400  # same attempt


def test_api_sealed_attempt_digest_is_enforced(client, api_env, tmp_path):
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
        output = results / 'output.csv'; output.write_text(sample(), newline="")
        evidence.seal_attempt(task, status='passed', verdict='PASS', message='fixture', evidence_kind='mock', runner_backend='mock')
        db.session.commit()
        ok = client.get(base).get_json()["data"]
        assert ok["attempts"] == [1] and ok["judge"]["available"] is False
        output.write_text('"time","x"\r\n0,987\r\n')
        assert client.get(base).status_code == 404


def test_saved_views_are_shared_validated_and_owned(client, api_env):
    project = api_env["project"]
    url = f"/api/v1/projects/{project}/curve-views"
    headers = {"X-CSRF-Token": "t"}
    login(client, api_env["reader"])
    layout = {"lanes": [{"name": "Rte_OUT_PP_Mode_u1_Mode", "height": 40}], "range": [0.4, 1.6]}
    created = client.post(url, json={"test_id": "TC-1", "name": "失败窗口", "layout": layout}, headers=headers)
    assert created.status_code == 201
    view_id = created.get_json()["data"]["view"]["id"]
    assert client.post(url, json={"test_id": "TC-1", "name": "失败窗口", "layout": layout}, headers=headers).status_code == 409
    for bad in ({"lanes": []}, {"lanes": [{"name": ""}]}, {"lanes": [{"name": "x", "height": 9}]},
                {"lanes": [{"name": "x"}], "range": [2, 1]}, {"lanes": [{"name": "x"}], "extra": 1}):
        assert client.post(url, json={"test_id": "TC-1", "name": "x", "layout": bad}, headers=headers).status_code == 400
    assert client.post(url, json={"test_id": "NOPE", "name": "x", "layout": layout}, headers=headers).status_code == 404
    login(client, api_env["reader2"])
    listed = client.get(url + "?test_id=TC-1").get_json()["data"]["views"]
    assert [v["name"] for v in listed] == ["失败窗口"] and listed[0]["layout"] == layout
    assert client.put(f"{url}/{view_id}", json={"name": "改名"}, headers=headers).status_code == 403
    assert client.delete(f"{url}/{view_id}", headers=headers).status_code == 403
    login(client, api_env["reader"])
    assert client.put(f"{url}/{view_id}", json={"name": "改名"}, headers=headers).get_json()["data"]["view"]["name"] == "改名"
    assert client.delete(f"{url}/{view_id}", headers=headers).status_code == 200
    login(client, api_env["outsider"])
    assert client.get(url + "?test_id=TC-1").status_code in (403, 404)


def test_curve_limits_are_runtime_settings(app_ctx):
    from app.services import runtime_config
    with app_ctx.app_context():
        keys = {f["key"] for f in runtime_config.describe() if f["group"] == "curves"}
        assert keys == {"curve_max_file_mb", "curve_point_limit", "curve_max_lanes", "curve_cache_mb"}
        runtime_config.set_many({"curve_max_lanes": 6})
        assert curves.limits().max_lanes == 6
