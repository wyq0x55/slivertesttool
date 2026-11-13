"""The per-run .sil must reproduce what the add_module/restart calls configured."""

import threading
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from app.runners.silver_runner import RunContext, RunnerError, SilverRunner, compose_run_sil

MODEL = b"""<workspace>
  <major-version>3</major-version>
  <modules>
    <gui-module/>
    <module>
      <uuid>host</uuid>
      <sil-line>C:/model/host.dll -S C:/model/host.sbs</sil-line>
      <enabled>true</enabled>
      <remote-module-cluster>SILVER_32_CLUSTER</remote-module-cluster>
    </module>
    <module>
      <uuid>old-judge</uuid>
      <sil-line>Python.dll ${JUDGEPY} -a "${JDGRSLT} ${SIlVER}"</sil-line>
      <enabled>true</enabled>
    </module>
  </modules>
  <parameters>
    <param><name>JDGRSLT</name><value>C:\\stale\\jdgrslt.log</value></param>
    <param><name>JUDGEPY</name><value>C:\\Work\\TC\\judge.py</value></param>
  </parameters>
  <working-dir></working-dir>
  <tabs><tab id="t" name="COM"/></tabs>
</workspace>"""


def _modules(root):
    return [(m.tag, m.findtext("sil-line"), m.findtext("enabled"))
            for m in root.find("modules")]


def test_compose_inserts_modules_after_gui_and_sets_parameters(tmp_path):
    out = ET.fromstring(compose_run_sil(
        MODEL, ["CsvWriter.dll ${OUTPUTCSV}", "Python.dll ${RUNNERPY}"],
        {"JDGRSLT": "C:\\run\\jdgrslt.log", "RUNNERPY": "C:\\run\\r.py"},
        working_dir=tmp_path))

    assert _modules(out) == [
        ("gui-module", None, None),
        ("module", "CsvWriter.dll ${OUTPUTCSV}", "true"),
        ("module", "Python.dll ${RUNNERPY}", "true"),
        ("module", "C:/model/host.dll -S C:/model/host.sbs", "true"),
        ("module", 'Python.dll ${JUDGEPY} -a "${JDGRSLT} ${SIlVER}"', "false"),
    ]
    params = {p.findtext("name"): p.findtext("value")
              for p in out.find("parameters")}
    assert params == {"JDGRSLT": "C:\\run\\jdgrslt.log",
                      "JUDGEPY": "C:\\Work\\TC\\judge.py",
                      "RUNNERPY": "C:\\run\\r.py"}
    assert out.findtext("working-dir") == str(tmp_path)
    assert out.find("modules")[3].findtext("remote-module-cluster") == "SILVER_32_CLUSTER"
    assert out.find("tabs/tab").get("name") == "COM"


def test_compose_keeps_an_explicit_working_dir(tmp_path):
    model = MODEL.replace(b"<working-dir></working-dir>", b"<working-dir>D:\\w</working-dir>")
    out = ET.fromstring(compose_run_sil(model, [], {}, working_dir=tmp_path))
    assert out.findtext("working-dir") == "D:\\w"


def test_compose_rejects_unreadable_model(tmp_path):
    with pytest.raises(RunnerError):
        compose_run_sil(b"\x00binary", [], {}, working_dir=tmp_path)


class _Instance:
    def __init__(self):
        self.calls = []

    def open(self, path):
        self.calls.append(("open", Path(path).name))

    def set_silver_property(self, name, value):
        self.calls.append(("property", name))

    def run_until(self, condition, timeout=0):
        self.calls.append(("run_until", condition))

    def __getattr__(self, name):  # add_module/restart/... must not be used
        raise AssertionError(f"unexpected Silver API call: {name}")


def test_pooled_run_loads_the_configuration_once(tmp_path):
    case = tmp_path / "inputs" / "TC1"
    case.mkdir(parents=True)
    (case / "silver_json_runner.py").write_text("", encoding="utf-8")
    (case / "testcase_TC1.json").write_text("{}", encoding="utf-8")
    (case / "output.txt").write_text("sig\n", encoding="utf-8")
    model = tmp_path / "model" / "RWS.sil"
    model.parent.mkdir()
    model.write_bytes(MODEL)
    original = model.read_bytes()
    log_dir = tmp_path / "logs"
    ctx = RunContext(test_id="TC1", run_dir=tmp_path / "inputs", log_dir=log_dir,
                     sil_path=model, gui=False, timeout=5,
                     cancel_event=threading.Event(), reload_model=True)

    instance = _Instance()
    SilverRunner().configure_and_run(instance, ctx)

    assert instance.calls == [("open", "run.sil"),
                              ("property", "scriptingApiLogDisabled"),
                              ("run_until", "isFinished != True")]
    assert model.read_bytes() == original  # pinned evidence stays untouched
    out = ET.fromstring((log_dir / "run.sil").read_bytes())
    lines = [m.findtext("sil-line") for m in out.find("modules").findall("module")]
    assert lines[:2] == ["CsvWriter.dll ${OUTPUTCSV} -l ${OUTPUTTXT} -w 0 -I 0.001 -m t",
                         'Python.dll ${RUNNERPY} -a "${JDGRSLT} ${TESTCASE}"']
    params = {p.findtext("name"): p.findtext("value") for p in out.find("parameters")}
    assert params["OUTPUTCSV"] == str(log_dir / "output.csv")
    assert params["TESTCASE"] == str(case / "testcase_TC1.json")
    assert params["OUTPUTTXT"] == str(case / "output.txt")
    assert params["SIlVER"] == str(model)
