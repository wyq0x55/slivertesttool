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
        return True

    def clear(self):
        self.calls.append(("clear",))

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
                              ("run_until", "isFinished != True"),
                              ("clear",)]
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


def test_prepare_run_sil_uses_model_dir_output_txt_fallback(tmp_path):
    case = tmp_path / "inputs" / "TC1"
    case.mkdir(parents=True)
    (case / "silver_json_runner.py").write_text("", encoding="utf-8")
    (case / "testcase_TC1.json").write_text("{}", encoding="utf-8")
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    model = model_dir / "RWS.sil"
    model.write_bytes(MODEL)
    model_signals = model_dir / "output.txt"
    model_signals.write_text("veh_speed\n", encoding="utf-8")

    log_dir = tmp_path / "logs"
    ctx = RunContext(test_id="TC1", run_dir=tmp_path / "inputs", log_dir=log_dir,
                     sil_path=model, gui=False, timeout=5)
    run_sil = SilverRunner().prepare_run_sil(ctx)
    out = ET.fromstring(run_sil.read_bytes())
    params = {p.findtext("name"): p.findtext("value") for p in out.find("parameters")}
    assert params["OUTPUTTXT"] == str(model_signals)


def test_csv_budget_guard_rejects_oversized_output_csv(tmp_path):
    case = tmp_path / "inputs" / "TC1"
    case.mkdir(parents=True)
    (case / "silver_json_runner.py").write_text("", encoding="utf-8")
    (case / "testcase_TC1.json").write_text("{}", encoding="utf-8")
    model = tmp_path / "model" / "RWS.sil"
    model.parent.mkdir()
    model.write_bytes(MODEL)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "output.csv").write_bytes(b"x" * 2048)
    ctx = RunContext(test_id="TC1", run_dir=tmp_path / "inputs", log_dir=log_dir,
                     sil_path=model, gui=False, timeout=5,
                     reload_model=True, max_csv_bytes=1024)

    with pytest.raises(RunnerError, match="output.csv size"):
        SilverRunner().configure_and_run(_Instance(), ctx)


def test_prepare_run_sil_materializes_project_default_signal_list_fallback(tmp_path):
    case = tmp_path / "inputs" / "TC1"
    case.mkdir(parents=True)
    (case / "silver_json_runner.py").write_text("", encoding="utf-8")
    (case / "testcase_TC1.json").write_text("{}", encoding="utf-8")
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    model = model_dir / "RWS.sil"
    model.write_bytes(MODEL)
    log_dir = tmp_path / "logs"

    ctx = RunContext(
        test_id="TC1",
        run_dir=tmp_path / "inputs",
        log_dir=log_dir,
        sil_path=model,
        gui=False,
        timeout=5,
        default_signal_list="\n  sig_speed \r\n\n sig_torque  \n",
    )
    run_sil = SilverRunner().prepare_run_sil(ctx)
    project_output = log_dir / "project_output.txt"
    assert project_output.is_file()
    assert project_output.read_text(encoding="utf-8") == "sig_speed\nsig_torque\n"

    out = ET.fromstring(run_sil.read_bytes())
    lines = [m.findtext("sil-line") for m in out.find("modules").findall("module")]
    assert lines[0] == "CsvWriter.dll ${OUTPUTCSV} -l ${OUTPUTTXT} -w 0 -I 0.001 -m t"
    params = {p.findtext("name"): p.findtext("value") for p in out.find("parameters")}
    assert params["OUTPUTTXT"] == str(project_output)

    # Model-dir output.txt still takes precedence over default_signal_list.
    model_signals = model_dir / "output.txt"
    model_signals.write_text("model_sig\n", encoding="utf-8")
    log_dir_2 = tmp_path / "logs2"
    ctx2 = RunContext(
        test_id="TC1",
        run_dir=tmp_path / "inputs",
        log_dir=log_dir_2,
        sil_path=model,
        gui=False,
        timeout=5,
        default_signal_list="sig_speed\n",
    )
    run_sil_2 = SilverRunner().prepare_run_sil(ctx2)
    assert not (log_dir_2 / "project_output.txt").exists()
    out_2 = ET.fromstring(run_sil_2.read_bytes())
    params_2 = {p.findtext("name"): p.findtext("value") for p in out_2.find("parameters")}
    assert params_2["OUTPUTTXT"] == str(model_signals)


def _install_fake_silver_sdk(root: Path) -> Path:
    import sys
    import textwrap

    packages = root / "common" / "ext-tools"
    if sys.platform.startswith("linux"):
        packages /= "linux64"
    packages /= "python3/lib/site-packages"
    module = packages / "synopsys/silver/remotescripting"
    module.mkdir(parents=True, exist_ok=True)
    (packages / "Pyro4.py").write_text(
        "from types import SimpleNamespace\nconfig = SimpleNamespace()\n",
        encoding="utf-8",
    )
    marker = root / "fake_native.pid"
    (module / "remotesilverapi.py").write_text(
        textwrap.dedent(
            f"""
            import os, subprocess, sys, time
            from pathlib import Path

            class LocalSilverNative:
                def __init__(self, **options):
                    self.process = subprocess.Popen(
                        [sys.executable, "-c", "import time; time.sleep(60)"]
                    )
                    Path({str(marker)!r}).write_text(str(self.process.pid))

                def ping(self, value):
                    return f"pong:{{value}}"

                def hang(self):
                    time.sleep(60)

                def crash_worker(self):
                    self.process.kill()
                    os._exit(1)

                def exit(self, **kwargs):
                    self.process.kill()
                    self.process.wait(timeout=5)
            """
        ),
        encoding="utf-8",
    )
    return marker


def test_silver_session_pure_pipe_poll_timeout_and_crash(tmp_path):
    import time
    import app.runners.silver_runner as sr

    assert not hasattr(sr, "_IpcWorker")
    _install_fake_silver_sdk(tmp_path)

    # 1. Normal synchronous RPC + timeout via _call(..., timeout=...)
    session = sr._SilverSession(str(tmp_path), {}, time.monotonic() + 10)
    try:
        assert not any(t.name == "silver-ipc" for t in threading.enumerate())
        assert session._call("ping", "ok", timeout=2.0) == "pong:ok"
        with pytest.raises(RunnerError, match="^timeout$"):
            session._call("hang", timeout=0.2)
        assert session._closed is True
    finally:
        session.stop()

    # 2. Abrupt worker exit mid-call raises "Silver API process exited" promptly.
    session2 = sr._SilverSession(str(tmp_path), {}, time.monotonic() + 10)
    try:
        with pytest.raises(RunnerError, match="Silver API process exited"):
            session2._call("crash_worker", timeout=3.0)
        assert session2._closed is True
    finally:
        session2.stop()
