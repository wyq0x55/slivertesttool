"""Silver execution backends.

The original ``RunTest.py`` logic is refactored here into a reusable,
per-job runner that executes inside an isolated workspace. Two backends are
provided:

* :class:`SilverRunner`   - the real Synopsys Silver backend (requires
  ``SILVER_HOME`` and the bundled ``synopsys`` / ``Pyro4`` packages). Heavy
  imports are performed lazily so the service can start on machines without
  Silver installed.
* :class:`MockSilverRunner` - a deterministic simulation that produces the
  same output-file layout. Used for CI, demos and machines without a Silver
  license, selected via ``runner_backend = mock`` in config.ini.

Both backends implement :meth:`SilverRunnerBase.run`, which receives a
:class:`RunContext` and writes result artefacts into ``ctx.log_dir``.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("silver.runner")


@dataclass
class RunContext:
    """Everything a runner needs to execute one test.

    Attributes
    ----------
    test_id:
        Logical identifier of the test case (formerly ``TestID``).
    run_dir:
        The assembled run directory. The ``.sil`` model sits at its root and
        the ``<test_id>/`` test-case folder is a sibling inside it. Because
        Silver resolves relative paths against the ``.sil`` file's directory,
        this layout guarantees the judge can be found at run time. The judge
        is a self-contained script (local library modules are inlined by the
        client at upload time), so no extra library folders are needed.
    log_dir:
        Directory where all result artefacts are written. Returned to the
        client afterwards.
    sil_path:
        Absolute path to the ``.sil`` model to open (located inside run_dir).
    gui:
        Whether to launch the Silver GUI (normally False on a server).
    timeout:
        Hard execution timeout in seconds.
    cancel_event:
        Set by the JobManager to request cancellation. Runners must honour it
        cooperatively and raise :class:`RunnerCancelled`.
    on_start:
        Optional callback invoked with the live Silver handle once it exists,
        so the JobManager can force-stop a running execution.
    reload_model:
        When True the runner is executing on a *reused* (pre-warmed pool)
        instance and must load a fresh copy of the model with ``open()`` before
        configuring the run. This both switches the model (if different from the
        one currently loaded) and discards any modules added by the previous
        run, giving a clean configuration without paying the process-launch
        cost. When False (the classic dedicated-instance path) the model is
        already open from the constructor and no reload is needed.
    console_log:
        Absolute path of the file Silver writes its console/log output to (its
        ``-l`` launch argument). For a dedicated instance this lives inside
        ``log_dir``; for a pooled instance it is a stable per-instance file that
        is sliced per run by the caller. When ``None`` the runner falls back to
        ``log_dir / "Console.log"``.
    """

    test_id: str
    run_dir: Path
    log_dir: Path
    sil_path: Path
    gui: bool
    timeout: int
    cancel_event: threading.Event = field(default_factory=threading.Event)
    on_start: Optional[Callable[[Any], None]] = None
    reload_model: bool = False
    console_log: Optional[Path] = None


class RunnerError(Exception):
    """Raised when a test execution fails irrecoverably."""


class RunnerCancelled(Exception):
    """Raised when an execution is cancelled at the user's request."""


class SilverRunnerBase:
    """Interface every backend implements."""

    def run(self, ctx: RunContext) -> None:  # pragma: no cover - interface
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Real Silver backend (faithful port of the original RunTest.py)
# --------------------------------------------------------------------------- #
class SilverRunner(SilverRunnerBase):
    """Execute a test with a real, dedicated Synopsys Silver instance.

    Two execution styles share the exact same configuration logic
    (:meth:`configure_and_run`):

    * **Dedicated instance** (:meth:`run`) - the classic path: launch a fresh
      Silver process for this one job and dispose it afterwards.
    * **Pre-warmed pool instance** - the platform launches ``license_count``
      empty Silver instances at start-up (each holding a license) and *reuses*
      them across jobs. Per run the pool borrows an idle instance, calls
      :meth:`configure_and_run` with ``ctx.reload_model=True`` (which reloads a
      fresh copy of the model via ``open()``), and returns the instance to the
      pool instead of exiting it. This removes the Silver process-launch cost
      from the critical path and pre-empts the licenses on start-up.

    Concurrency is bounded by the license count (the pool size / DB license
    gate), not here.
    """

    # ---------------------- lifecycle helpers ------------------------------- #
    def launch(self, sil_path: Path, gui: bool, console_log: Path) -> Any:
        """Launch a Silver instance opened on *sil_path*.

        Used both for the dedicated-instance path and to pre-warm pool
        instances. ``console_log`` becomes the instance's ``-l`` output file;
        for pooled instances it is a stable per-instance path that is sliced
        per run by the caller.
        """
        silver_home = os.environ.get("SILVER_HOME")
        if not silver_home:
            raise RunnerError("SILVER_HOME environment variable is not set.")
        if not Path(sil_path).is_file():
            raise RunnerError(f"SIL model not found: {sil_path}")

        LocalSilverNative, _Pyro4 = self._import_silver(silver_home)

        console_log = Path(console_log)
        console_log.parent.mkdir(parents=True, exist_ok=True)
        console_log.write_text("", encoding="utf-8")

        logger.info("Launching Silver instance on model '%s'", sil_path)
        silver = LocalSilverNative(
            sil=str(sil_path),
            timeout=1000,
            stopped=True,
            speedup=False,
            gui=gui,
            args=[f"-l {console_log}"],
            silent=False,
            backend="native",
        )
        return silver

    @staticmethod
    def open_model(silver: Any, sil_path: Path) -> None:
        """Load a fresh copy of *sil_path* into an already-running instance.

        ``open()`` reloads the whole configuration, so it both switches the
        model (when different) and discards any modules a previous run added,
        yielding a clean configuration without a process restart.
        """
        silver.open(str(sil_path))

    def generate_sil(self, sil_path: Path, dll_ref: str, sbs_ref: str,
                     index: int = 3) -> Path:
        """Generate a fresh ``.sil`` holding a single ``<dll> -S <sbs>`` module.

        Uses the real Silver API rather than hand-writing the file: an empty
        configuration is created in memory (``LocalSilverNative(sil=None)``),
        the module line is injected with ``add_module`` and the configuration
        is persisted with ``save()``. ``dll_ref`` / ``sbs_ref`` are the exact
        strings written into the module line: the caller passes the dll and sbs
        paths *relative to the Silver working directory* (e.g.
        ``instance/model/project_1/host/host.dll``) so Silver resolves them at
        run time. The matching ``.pdb`` must sit next to the dll on disk.
        """
        silver_home = os.environ.get("SILVER_HOME")
        if not silver_home:
            raise RunnerError("SILVER_HOME environment variable is not set.")

        sil_path = Path(sil_path)
        sil_path.parent.mkdir(parents=True, exist_ok=True)

        LocalSilverNative, _Pyro4 = self._import_silver(silver_home)

        sil_line = f"{dll_ref} -S {sbs_ref}"
        logger.info("Generating .sil '%s' with module '%s'", sil_path, sil_line)
        silver = LocalSilverNative(
            sil=None,
            timeout=1000,
            stopped=True,
            speedup=False,
            gui=False,
            silent=True,
            backend="native",
        )
        try:
            moduleuuid = silver.add_module(index=index, sil_line=sil_line)
            silver.set_module_property(moduleuuid,"remote_cluster","host32")
            silver.save(str(sil_path))
        finally:
            self._safe_dispose(silver)

        if not sil_path.is_file():
            raise RunnerError(f"Silver did not write the model: {sil_path}")
        return sil_path

    def run(self, ctx: RunContext) -> None:
        """Dedicated-instance path: launch, run, dispose."""
        console_log = ctx.console_log or (ctx.log_dir / "Console.log")
        ctx.log_dir.mkdir(parents=True, exist_ok=True)

        if ctx.cancel_event.is_set():
            raise RunnerCancelled("Cancelled before Silver launch.")

        silver = self.launch(ctx.sil_path, ctx.gui, console_log)
        # Publish the handle so the JobManager can force-stop this run.
        if ctx.on_start is not None:
            ctx.on_start(silver)
        try:
            self.configure_and_run(silver, ctx)
        finally:
            self._safe_dispose(silver)

    def configure_and_run(self, silver: Any, ctx: RunContext) -> None:
        """Configure the (already-running) instance for one job and run it.

        Works on both a freshly-launched dedicated instance and a reused pool
        instance. When ``ctx.reload_model`` is set the model is re-opened first
        so the configuration is clean. The caller is responsible for publishing
        the handle (``ctx.on_start``) and for disposing / returning the
        instance afterwards.
        """
        ctx.log_dir.mkdir(parents=True, exist_ok=True)
        output_csv = ctx.log_dir / "output.csv"
        # CsvWriter's ``-l`` argument is a *signal-selection list* (which signals
        # to log). It is an INPUT that ships inside the test-case folder, not an
        # artefact we produce. Resolve it there; if the test case does not
        # provide one, we omit ``-l`` entirely so CsvWriter logs all variables
        # instead of failing to open a non-existent file in ``logs``.
        testcase_dir = ctx.run_dir / ctx.test_id
        signal_list = testcase_dir / "output.txt"
        judge_py = testcase_dir / "judge.py"
        jdgrslt = ctx.log_dir / "jdgrslt.log"

        # JSON-runner mode: when the test-case folder ships the vendored
        # ``silver_json_runner.py`` (materialised from DB data), execute it in
        # place of ``judge.py``. It reads ``testcase_<id>.json`` (given on the
        # command line) plus ``constants.json`` / ``lib.json`` beside it. Its
        # console output is byte-for-byte judge-compatible, so the downstream
        # verdict parser is unchanged.
        runner_py = testcase_dir / "silver_json_runner.py"
        json_mode = runner_py.is_file()
        testcase_json = None
        if json_mode:
            candidates = sorted(testcase_dir.glob("testcase_*.json"))
            if not candidates:
                raise RunnerError(
                    f"silver_json_runner present but no testcase_*.json in "
                    f"{testcase_dir}")
            testcase_json = candidates[0]

        if not ctx.sil_path.is_file():
            raise RunnerError(f"SIL model not found: {ctx.sil_path}")
        if not json_mode and not judge_py.is_file():
            raise RunnerError(f"judge.py not found: {judge_py}")

        if ctx.cancel_event.is_set():
            raise RunnerCancelled("Cancelled before configuration.")

        # Reused pool instance: reload a fresh copy of the model. This switches
        # the model if needed and drops the previous run's injected modules.
        if ctx.reload_model:
            logger.info("Reusing pooled Silver instance for test '%s' "
                        "(reloading model '%s')", ctx.test_id, ctx.sil_path)
            self.open_model(silver, ctx.sil_path)

        silver.set_silver_property("scriptingApiLogDisabled", True)

        # Configure Silver parameters (paths are absolute per-job).
        silver.set_config_parameter("OUTPUTCSV", str(output_csv))
        silver.set_config_parameter("JDGRSLT", str(jdgrslt))
        silver.set_config_parameter("SIlVER", str(ctx.sil_path))
        if json_mode:
            silver.set_config_parameter("RUNNERPY", str(runner_py))
            silver.set_config_parameter("TESTCASE", str(testcase_json))
        else:
            silver.set_config_parameter("JUDGEPY", str(judge_py))

        # Disable any pre-existing judge/runner/output modules, then inject ours.
        for uuid in silver.get_module_uuids():
            prop = silver.get_module_properties(uuid)
            line = prop.get("preview_sil_line", "")
            if (("judge.py" in line) or ("silver_json_runner.py" in line)
                    or ("output.csv" in line)) and prop.get("enabled"):
                silver.set_module_property(uuid, "enabled", False)

        # Only pass ``-l <signal list>`` when the test case actually ships
        # one; otherwise CsvWriter logs all variables (no missing-file error).
        if signal_list.is_file():
            silver.set_config_parameter("OUTPUTTXT", str(signal_list))
            csv_line = ("CsvWriter.dll ${OUTPUTCSV} -l ${OUTPUTTXT} "
                        "-w 0 -I 0.001 -m t")
        else:
            logger.info(
                "No signal-selection file at %s; CsvWriter will log all "
                "variables.", signal_list,
            )
            csv_line = "CsvWriter.dll ${OUTPUTCSV} -w 0 -I 0.001 -m t"
        silver.add_module(index=1, sil_line=csv_line)
        if json_mode:
            # argv[1]=log file (as judge), argv[2]=test-case JSON; constants
            # and lib are auto-resolved from beside the runner.
            judge_line = 'Python.dll ${RUNNERPY} -a "${JDGRSLT} ${TESTCASE}"'
        else:
            judge_line = 'Python.dll ${JUDGEPY} -a "${JDGRSLT} ${SIlVER}"'
        silver.add_module(index=2, sil_line=judge_line)
        silver.restart()

        if ctx.cancel_event.is_set():
            raise RunnerCancelled("Cancelled before run.")

        try:
            # Blocking call. A concurrent JobManager.cancel() invokes
            # silver.exit(force=True) from another thread, which unblocks
            # run_until; we then detect the cancel flag below.
            silver.run_until("isFinished != True", timeout=ctx.timeout)
        except (RuntimeError, SystemError, ValueError) as exc:
            if ctx.cancel_event.is_set():
                raise RunnerCancelled("Cancelled during run.") from exc
            logger.warning("Silver run raised %s: %s", type(exc).__name__, exc)
            raise RunnerError(f"Execution error: {exc}") from exc

        if ctx.cancel_event.is_set():
            raise RunnerCancelled("Cancelled during run.")

    @staticmethod
    def _import_silver(silver_home: str):
        """Lazily import the Silver remote-scripting API and Pyro4."""
        remote_scripting_path = os.path.join(
            silver_home, "common", "ext-tools",
            "linux64" if sys.platform.startswith("linux") else "",
            "python3", "lib", "site-packages", "synopsys",
        )
        parent = str(Path(remote_scripting_path).parent)
        if parent not in sys.path:
            sys.path.append(parent)
        pyro_zip = os.path.join(silver_home, "lib", "Pyro4.zip")
        if pyro_zip not in sys.path:
            sys.path.append(pyro_zip)

        import Pyro4  # type: ignore
        from synopsys.silver.remotescripting.remotesilverapi import (  # type: ignore
            LocalSilverNative,
        )

        Pyro4.config.SERIALIZERS_ACCEPTED = ["pickle"]
        Pyro4.config.SERIALIZER = "pickle"
        Pyro4.config.PICKLE_PROTOCOL_VERSION = 2
        return LocalSilverNative, Pyro4

    @staticmethod
    def _safe_dispose(silver) -> None:
        """Exit the Silver instance (LocalSilverNative.exit closes everything)."""
        exit_fn = getattr(silver, "exit", None)
        if callable(exit_fn):
            try:
                exit_fn(force=True)
                return
            except Exception:  # noqa: BLE001 - best effort cleanup
                pass
        # Fallback for any other backend object.
        for method in ("close", "shutdown", "dispose", "quit"):
            fn = getattr(silver, method, None)
            if callable(fn):
                try:
                    fn()
                    return
                except Exception:  # noqa: BLE001
                    continue

    @staticmethod
    def force_stop(silver) -> None:
        """Force-terminate a running Silver instance (called on cancellation)."""
        try:
            silver.exit(force=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("force_stop failed: %s", exc)


# --------------------------------------------------------------------------- #
# Mock backend (no license required)
# --------------------------------------------------------------------------- #
class MockHandle:
    """A stand-in for a live Silver handle used by the mock backend.

    It mirrors the small slice of the real ``LocalSilverNative`` API the pool
    relies on (``open`` / ``exit``) so a mock instance can be pre-warmed,
    reused and disposed exactly like a real one.
    """

    def __init__(self, sil_path: Any = None) -> None:
        self.sil_path = str(sil_path) if sil_path is not None else None
        self.alive = True

    def open(self, file_name: str) -> None:
        self.sil_path = str(file_name)

    def exit(self, force: bool = False) -> None:
        self.alive = False


class MockSilverRunner(SilverRunnerBase):
    """Simulate a Silver run, producing the same output file layout."""

    def __init__(self, delay_seconds: float = 0.5) -> None:
        self._delay = delay_seconds

    # ---------------------- lifecycle helpers ------------------------------- #
    def launch(self, sil_path: Path, gui: bool, console_log: Path) -> MockHandle:
        """Create a lightweight mock instance (no real process)."""
        return MockHandle(sil_path)

    @staticmethod
    def open_model(handle: MockHandle, sil_path: Path) -> None:
        handle.open(str(sil_path))

    def run(self, ctx: RunContext) -> None:
        """Dedicated-instance path for the mock backend."""
        handle = MockHandle(ctx.sil_path)
        if ctx.on_start is not None:
            ctx.on_start(handle)
        self.configure_and_run(handle, ctx)

    def configure_and_run(self, handle: MockHandle, ctx: RunContext) -> None:
        """Simulate one job on the given (reusable) mock handle."""
        ctx.log_dir.mkdir(parents=True, exist_ok=True)
        testcase_dir = ctx.run_dir / ctx.test_id
        judge_py = testcase_dir / "judge.py"
        runner_py = testcase_dir / "silver_json_runner.py"
        json_mode = runner_py.is_file()

        if ctx.reload_model:
            self.open_model(handle, ctx.sil_path)

        # Emulate execution time while remaining cancellable.
        deadline = time.time() + self._delay
        while time.time() < deadline:
            if ctx.cancel_event.is_set():
                raise RunnerCancelled("Cancelled during mock run.")
            time.sleep(0.05)

        (ctx.log_dir / "Console.log").write_text(
            f"[MOCK] Silver launched for test '{ctx.test_id}'\n"
            f"[MOCK] sil model: {ctx.sil_path}\n"
            f"[MOCK] run dir: {ctx.run_dir}\n"
            f"[MOCK] json runner: {json_mode}\n"
            f"[MOCK] judge.py present: {judge_py.is_file()}\n"
            f"[MOCK] execution finished\n",
            encoding="utf-8",
        )
        (ctx.log_dir / "output.csv").write_text(
            "time,signal_a,signal_b\n0.000,0,0\n0.001,1,0\n0.002,1,1\n",
            encoding="utf-8",
        )
        (ctx.log_dir / "output.txt").write_text(
            "time signal_a signal_b\n0.000 0 0\n0.001 1 0\n0.002 1 1\n",
            encoding="utf-8",
        )
        if json_mode:
            # Emit judge-compatible markers so the real verdict parser resolves
            # this to PASS (the JSON runner produces the same markers on Silver).
            (ctx.log_dir / "jdgrslt.log").write_text(
                f"Test case ID.ID;;{ctx.test_id} is started!\n"
                "Step.1 is passed\n"
                "All steps are verified.Test is Passed.\n",
                encoding="utf-8",
            )
        else:
            verdict = "PASS" if judge_py.is_file() else "PASS(no-judge)"
            (ctx.log_dir / "jdgrslt.log").write_text(
                f"test_id={ctx.test_id}\nverdict={verdict}\n",
                encoding="utf-8",
            )
        logger.info("[MOCK] completed test '%s'", ctx.test_id)


def build_runner(backend: str, **kwargs) -> SilverRunnerBase:
    """Factory selecting a runner backend by name."""
    backend = (backend or "silver").strip().lower()
    if backend == "mock":
        return MockSilverRunner(**kwargs)
    if backend == "silver":
        return SilverRunner()
    raise ValueError(f"Unknown runner backend: {backend}")
