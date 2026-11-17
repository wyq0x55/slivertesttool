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

import concurrent.futures
import contextvars
import logging
import multiprocessing
import os
import queue
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .process_guard import (
    kill_process_tree, pid_is_running, require_process_job, retain_cleanup,
    release_cleanup, retry_pending_cleanup,
)

logger = logging.getLogger("silver.runner")
_launch_deadline = contextvars.ContextVar("silver_launch_deadline", default=None)


def _silver_worker(connection, silver_home: str, options: dict) -> None:
    """Keep all native API calls inside a process that can be terminated."""
    if os.name != "nt":
        os.setsid()
    connection.send(("ready", None))
    try:
        connection.recv()
        native, _pyro = SilverRunner._import_silver(silver_home)
        silver = native(**options)
        connection.send(("ok", silver.process.pid))
        while True:
            if not connection.poll(0.1):
                if silver.process.poll() is not None:
                    return
                continue
            name, args, kwargs = connection.recv()
            try:
                result = getattr(silver, name)(*args, **kwargs)
                connection.send(("ok", result))
            except Exception as exc:
                connection.send(("error", f"{type(exc).__name__}: {exc}"))
            if name == "exit":
                return
    except EOFError:
        pass
    except Exception as exc:
        connection.send(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        connection.close()


class _SilverProcess:
    """Expose the local liveness interface used by the instance pool."""

    def __init__(self, owner) -> None:
        self.owner = owner

    def poll(self):
        owner = self.owner
        if owner._worker.exitcode is not None:
            return owner._worker.exitcode
        if owner._native_pid is not None and not pid_is_running(owner._native_pid):
            return 1
        return None

    def wait(self):
        self.owner.stop()
        return self.owner._worker.exitcode


class _IpcWorker:
    """Single-thread daemon executor for deadline-bounded pipe I/O."""

    def __init__(self) -> None:
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._closed = False
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="silver-ipc"
        )
        self._thread.start()

    def _loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            fn, fut = item
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fut.set_result(fn())
            except BaseException as exc:
                fut.set_exception(exc)

    def run(self, fn: Callable[[], Any], timeout: float) -> Any:
        if timeout <= 0:
            raise RunnerError("timeout")
        if self._closed:
            raise RunnerError("Silver API process exited")
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._queue.put((fn, fut))
        try:
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            fut.cancel()
            raise RunnerError("timeout") from exc

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._queue.put(None)


class _SilverSession:
    """Synchronous API proxy with an absolute deadline and tree ownership."""

    def __init__(self, silver_home: str, options: dict, deadline: float) -> None:
        retry_pending_cleanup()
        context = multiprocessing.get_context("spawn")
        self._connection, remote = context.Pipe()
        self._worker = context.Process(
            target=_silver_worker, args=(remote, silver_home, options))
        self._job = None
        self._native_pid = None
        self._deadline = deadline
        self._lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._stopped = threading.Event()
        self._ipc = _IpcWorker()
        self.process = _SilverProcess(self)
        self._worker.start()
        remote.close()
        try:
            self._job = require_process_job(self._worker.pid)
            self._receive()
            self._transport(lambda: self._connection.send("start"))
            self._native_pid = self._receive()
        except BaseException as failure:
            try:
                self.stop()
            except Exception as cleanup:
                raise SilverLaunchCleanupError(
                    f"Silver launch failed and cleanup is unconfirmed: {cleanup}",
                    self,
                ) from failure
            raise

    def _receive(self):
        try:
            while True:
                if self._stopped.is_set():
                    raise RunnerError("Silver API process exited")
                remaining = self._deadline - time.monotonic()
                if remaining <= 0:
                    raise RunnerError("timeout")
                if self._connection.poll(min(0.25, remaining)):
                    break
            status, value = self._transport(self._connection.recv)
        except (EOFError, OSError) as exc:
            raise RunnerError("Silver API process exited") from exc
        if status == "error":
            raise RunnerError(f"Execution error: {value}")
        return value

    def _transport(self, operation):
        remaining = self._deadline - time.monotonic()
        return self._ipc.run(operation, remaining)

    def call(self, name, *args, **kwargs):
        remaining = max(0, self._deadline - time.monotonic())
        if not self._lock.acquire(timeout=remaining):
            raise RunnerError("timeout")
        try:
            try:
                if time.monotonic() >= self._deadline:
                    raise RunnerError("timeout")
                self._transport(lambda: self._connection.send((name, args, kwargs)))
                return self._receive()
            except (EOFError, OSError) as exc:
                self.stop()
                raise RunnerError("Silver API process exited") from exc
            except BaseException:
                self.stop()
                raise
        finally:
            self._lock.release()

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *args, **kwargs: self.call(name, *args, **kwargs)

    def stop(self, *, close_connection: bool = True) -> None:
        if not self._stop_lock.acquire(timeout=5):
            raise RunnerError("Silver API cleanup is still in progress")
        try:
            self._stopped.set()
            failures = []
            actions = []
            if os.name != "nt" or self._job is not None:
                actions.append(lambda: kill_process_tree(self._worker.pid, self._job))
            actions.extend((
                lambda: self._worker.kill() if self._worker.is_alive() else None,
                lambda: self._worker.join(timeout=2),
            ))
            if close_connection:
                actions.append(self._ipc.close)
                actions.append(self._connection.close)
            for action in actions:
                try:
                    action()
                except Exception as exc:
                    failures.append(exc)
            if self._worker.is_alive():
                failures.append(RunnerError("Silver API process could not be reaped"))
            if failures:
                retain_cleanup(self, self.stop)
                raise RunnerError("Silver API cleanup failed; ownership retained") from failures[0]
            release_cleanup(self)
        finally:
            self._stop_lock.release()


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
    max_csv_bytes: int = 0


class RunnerError(Exception):
    """Raised when a test execution fails irrecoverably."""


class SilverLaunchCleanupError(RunnerError):
    """A failed launch whose owned process still needs cleanup."""

    def __init__(self, message: str, handle) -> None:
        super().__init__(message)
        self.handle = handle


# Used only when a call has no job timeout and EXECUTION_TIMEOUT is unset.
_DEFAULT_SILVER_CALL_TIMEOUT = 1000


class RunnerCancelled(Exception):
    """Raised when an execution is cancelled at the user's request."""


#: Modules a run injects itself; any copy already saved in the model is disabled.
_INJECTED_MARKERS = ("judge.py", "silver_json_runner.py", "output.csv")


def compose_run_sil(model: bytes, module_lines: list[str], params: dict[str, str],
                    *, working_dir: Path) -> bytes:
    """Return *model* (a saved ``.sil``) configured for one run.

    Mirrors what the scripting API calls used to do on a live instance:
    enabled modules whose line references a judge/runner/``output.csv`` are
    disabled, *module_lines* are inserted right after the GUI module (the
    positions ``add_module(index=1/2)`` used), and *params* are set as
    configuration parameters. An empty ``<working-dir>`` is pinned to
    *working_dir* because the file is written outside the model directory.
    """
    import re
    import uuid
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(model)
    except ET.ParseError as exc:
        raise RunnerError(f"SIL model is not a readable configuration: {exc}") from exc
    modules = root.find("modules")
    if modules is None:
        raise RunnerError("SIL model has no <modules> section")
    parameters = root.find("parameters")
    if parameters is None:
        parameters = ET.SubElement(root, "parameters")
    existing = {param.findtext("name"): param for param in parameters.findall("param")}
    for name, value in params.items():
        param = existing.get(name)
        if param is None:
            param = ET.SubElement(parameters, "param")
            ET.SubElement(param, "name").text = name
        node = param.find("value")
        if node is None:
            node = ET.SubElement(param, "value")
        node.text = value

    # Match on the expanded line, as ``preview_sil_line`` did: saved lines
    # usually reference the judge through a parameter (``${JUDGEPY}``).
    values = {param.findtext("name"): param.findtext("value") or ""
              for param in parameters.findall("param")}
    for module in modules.findall("module"):
        line = re.sub(r"\$\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)),
                      module.findtext("sil-line") or "")
        enabled = module.find("enabled")
        if (any(marker in line for marker in _INJECTED_MARKERS)
                and enabled is not None and enabled.text == "true"):
            enabled.text = "false"
    gui = modules.find("gui-module")
    position = list(modules).index(gui) + 1 if gui is not None else 0
    for offset, line in enumerate(module_lines):
        module = ET.Element("module")
        for tag, text in (("uuid", str(uuid.uuid4())), ("sil-line", line),
                          ("enabled", "true"), ("parallel", "false")):
            ET.SubElement(module, tag).text = text
        modules.insert(position + offset, module)

    workdir = root.find("working-dir")
    if workdir is None:
        workdir = ET.SubElement(root, "working-dir")
    if not (workdir.text or "").strip():
        workdir.text = str(working_dir)
    return ET.tostring(root, encoding="utf-8")


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
    @staticmethod
    def _call_timeout(explicit: Optional[int] = None) -> int:
        """Per-call ceiling for Silver launch, open, and exit.

        A job supplies its execution timeout. Pool warmup and model generation
        have no job, so they use ``EXECUTION_TIMEOUT`` and otherwise a named
        one-call ceiling. ``timeout=1000`` is no longer buried in each call.
        """
        if explicit is not None and explicit > 0:
            return int(explicit)
        raw = os.environ.get("EXECUTION_TIMEOUT", "")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 0
        return value if value > 0 else _DEFAULT_SILVER_CALL_TIMEOUT

    def launch(self, sil_path: Path, gui: bool, console_log: Path,
               call_timeout: Optional[int] = None) -> Any:
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

        console_log = Path(console_log)
        console_log.parent.mkdir(parents=True, exist_ok=True)
        console_log.write_text("", encoding="utf-8")

        logger.info("Launching Silver instance on model '%s'", sil_path)
        bound = self._call_timeout(call_timeout)
        deadline = _launch_deadline.get() or (time.monotonic() + bound)
        silver = _SilverSession(silver_home, dict(
            sil=str(sil_path),
            timeout=bound,
            stopped=True,
            speedup=False,
            gui=gui,
            args=[f"-l {console_log}"],
            silent=False,
            backend="native",
        ), deadline)
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

        sil_line = f"{dll_ref} -S {sbs_ref}"
        logger.info("Generating .sil '%s' with module '%s'", sil_path, sil_line)
        bound = self._call_timeout(None)
        silver = _SilverSession(silver_home, dict(
            sil=None,
            timeout=bound,
            stopped=True,
            speedup=False,
            gui=False,
            silent=True,
            backend="native",
        ), time.monotonic() + bound)
        try:
            moduleuuid = silver.add_module(index=index, sil_line=sil_line)
            silver.set_module_property(moduleuuid, "remote_cluster", "host32")
            # macroStep (macro time step) + speedup (real-time factor) are
            # GLOBAL config properties -> emitted as top-level <property .../>.
            silver.set_silver_property("macroStep", "0.001")
            silver.set_silver_property("speedup", "0.0")
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
        run_sil = self.prepare_run_sil(ctx)

        if ctx.cancel_event.is_set():
            raise RunnerCancelled("Cancelled before Silver launch.")

        # Launch directly on the fully configured model so Silver initializes
        # the simulation exactly once.
        token = _launch_deadline.set(time.monotonic() + self._call_timeout(ctx.timeout))
        try:
            silver = self.launch(run_sil, ctx.gui, console_log)
        finally:
            _launch_deadline.reset(token)
        try:
            if ctx.on_start is not None:
                ctx.on_start(silver)
            self._execute(silver, ctx)
        finally:
            self._safe_dispose(silver)

    def configure_and_run(self, silver: Any, ctx: RunContext) -> None:
        """Configure a reused pool instance for one job and run it.

        The job's modules and parameters are written into a per-run copy of the
        model (:meth:`prepare_run_sil`) and loaded with a single ``open()``,
        which also switches the model if needed and drops the previous run's
        modules. The caller publishes the handle (``ctx.on_start``) and returns
        the instance afterwards. Clear the model before returning so native
        writers finish before the caller snapshots the results.
        """
        run_sil = self.prepare_run_sil(ctx)
        if ctx.cancel_event.is_set():
            raise RunnerCancelled("Cancelled before configuration.")
        logger.info("Reusing pooled Silver instance for test '%s' "
                    "(loading '%s')", ctx.test_id, run_sil)
        if isinstance(silver, _SilverSession):
            silver._deadline = time.monotonic() + self._call_timeout(ctx.timeout)
        try:
            self.open_model(silver, run_sil)
        except RunnerError as exc:
            if ctx.cancel_event.is_set():
                raise RunnerCancelled("Cancelled during configuration.") from exc
            raise
        completed = False
        try:
            self._execute(silver, ctx)
            completed = True
        finally:
            try:
                silver.clear()
            except Exception as exc:
                if completed:
                    if ctx.cancel_event.is_set():
                        raise RunnerCancelled("Cancelled during model unload.") from exc
                    raise RunnerError(f"Failed to unload Silver model: {exc}") from exc
                logger.warning("Silver model cleanup failed after execution failure: %s", exc)
        if ctx.cancel_event.is_set():
            raise RunnerCancelled("Cancelled during model unload.")

    def prepare_run_sil(self, ctx: RunContext) -> Path:
        """Write ``ctx.log_dir/run.sil``: the model plus this job's modules.

        Injecting CsvWriter and the judge/runner through the scripting API
        (``add_module`` twice, then ``restart()``) re-initialized the whole
        simulation once per call, loading every model DLL and re-checking the
        license each time: about 13 of a typical 17 seconds per run passed
        before the first test step. Writing the same modules and parameters
        into the configuration lets Silver initialize once. The pinned evidence
        model is left untouched (its hashes are verified later), and this copy
        is archived with the run's results.
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
        if not signal_list.is_file():
            model_signal_list = ctx.sil_path.parent / "output.txt"
            if model_signal_list.is_file():
                signal_list = model_signal_list
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

        params = {
            "OUTPUTCSV": str(output_csv),
            "JDGRSLT": str(jdgrslt),
            "SIlVER": str(ctx.sil_path),
        }
        if json_mode:
            params["RUNNERPY"] = str(runner_py)
            params["TESTCASE"] = str(testcase_json)
        else:
            params["JUDGEPY"] = str(judge_py)

        # Only pass ``-l <signal list>`` when the test case actually ships
        # one; otherwise CsvWriter logs all variables (no missing-file error).
        if signal_list.is_file():
            params["OUTPUTTXT"] = str(signal_list)
            csv_line = ("CsvWriter.dll ${OUTPUTCSV} -l ${OUTPUTTXT} "
                        "-w 0 -I 0.001 -m t")
        else:
            logger.info(
                "No signal-selection file at %s; CsvWriter will log all "
                "variables.", signal_list,
            )
            csv_line = "CsvWriter.dll ${OUTPUTCSV} -w 0 -I 0.001 -m t"
        if json_mode:
            # argv[1]=log file (as judge), argv[2]=test-case JSON; constants
            # and lib are auto-resolved from beside the runner.
            judge_line = 'Python.dll ${RUNNERPY} -a "${JDGRSLT} ${TESTCASE}"'
        else:
            judge_line = 'Python.dll ${JUDGEPY} -a "${JDGRSLT} ${SIlVER}"'

        run_sil = ctx.log_dir / "run.sil"
        run_sil.write_bytes(compose_run_sil(
            ctx.sil_path.read_bytes(), [csv_line, judge_line], params,
            working_dir=ctx.sil_path.parent))
        return run_sil

    def _execute(self, silver: Any, ctx: RunContext) -> None:
        """Run the loaded configuration to completion."""
        if ctx.cancel_event.is_set():
            raise RunnerCancelled("Cancelled before run.")

        console = ctx.console_log or (ctx.log_dir / "Console.log")
        try:
            console_offset = console.stat().st_size
        except FileNotFoundError:
            console_offset = 0
        except OSError:
            console_offset = None

        try:
            silver.set_silver_property("scriptingApiLogDisabled", True)
            completed = silver.run_until("isFinished != True", timeout=ctx.timeout)
        except (RunnerError, RuntimeError, SystemError, ValueError) as exc:
            if ctx.cancel_event.is_set():
                raise RunnerCancelled("Cancelled during run.") from exc
            if isinstance(exc, RunnerError):
                raise
            logger.warning("Silver run raised %s: %s", type(exc).__name__, exc)
            raise RunnerError(f"Execution error: {exc}") from exc

        if ctx.cancel_event.is_set():
            raise RunnerCancelled("Cancelled during run.")

        if not completed and not self._judge_stopped(ctx, console, console_offset):
            raise RunnerError("timeout")
        self._enforce_csv_budget(ctx)

    @staticmethod
    def _enforce_csv_budget(ctx: RunContext) -> None:
        if not ctx.max_csv_bytes or ctx.max_csv_bytes <= 0:
            return
        output_csv = ctx.log_dir / "output.csv"
        try:
            size = output_csv.stat().st_size if output_csv.is_file() else 0
        except OSError:
            size = 0
        if size > ctx.max_csv_bytes:
            raise RunnerError(
                f"output.csv size ({size} bytes) exceeds limit ({ctx.max_csv_bytes} bytes); "
                "provide an output.txt signal allowlist or increase SILVER_MAX_CSV_BYTES."
            )

    @staticmethod
    def _judge_stopped(ctx: RunContext, console: Path, offset: Optional[int]) -> bool:
        """Recognize this run's normal DLL_END, which run_until also returns as False."""
        if offset is None:
            return False
        module = "silver_json_runner.py" if (
            ctx.run_dir / ctx.test_id / "silver_json_runner.py"
        ).is_file() else "judge.py"
        record = (
            f"WARNING Silver: Simulation ended by 'Python.dll: {module} (Python3)' "
            "during modify_outputs()"
        ).encode("ascii")
        try:
            with console.open("rb") as stream:
                stream.seek(offset)
                return any(line.rstrip(b"\r\n") == record for line in stream)
        except OSError:
            return False

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
        if isinstance(silver, _SilverSession):
            try:
                silver._deadline = min(silver._deadline, time.monotonic() + 5)
                if silver.process.poll() is None and silver._lock.acquire(blocking=False):
                    silver._lock.release()
                    silver.exit(force=True)
            except Exception:
                logger.warning("Silver exit failed; terminating its owned process tree")
            finally:
                silver.stop()
            return
        process = getattr(silver, "process", None)
        try:
            exited = process is not None and process.poll() is not None
        except Exception:
            exited = False
        if exited:
            try:
                silver.callback_server.close()
            except Exception:
                logger.exception("Error closing callback server for exited Silver process")
            finally:
                try:
                    process.wait()
                except Exception:
                    logger.exception("Error reaping exited Silver process")
            return
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
        if isinstance(silver, _SilverSession):
            silver.stop(close_connection=False)
            return
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
        SilverRunner._enforce_csv_budget(ctx)
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
            # Same marker grammar as the json_mode branch: the non-JSON path
            # must resolve to PASS through the same verdict parser. A bare
            # ``verdict=PASS`` line is a mock-only format the parser has never
            # understood (it reads as UNKNOWN and fails the task). The
            # no-judge distinction lives in Console.log, not in the grammar.
            (ctx.log_dir / "jdgrslt.log").write_text(
                f"Test case ID.ID;;{ctx.test_id} is started!\n"
                + ("Step.1 is passed\n" if judge_py.is_file() else "")
                + "All steps are verified.Test is Passed.\n",
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
