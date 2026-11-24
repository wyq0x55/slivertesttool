"""Subprocess client for ``python -m ut_agent bridge`` (contract ``ut-agent.bridge/1``).

ut-agent runs in its own interpreter and checkout; this platform never imports
it.  Each call prints one JSON document; ``ok: false`` or a missing/foreign
contract raises :class:`BridgeError` so callers can route the job to a human.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from app.runners.process_guard import (
    kill_process_tree, popen_owned, release_cleanup, retain_cleanup,
)

CONTRACT = "ut-agent.bridge/1"
# Seconds between budget-forecast observations of a running generation.  The
# observation is read-only: it never feeds back into generation, and a failed
# or slow poll is dropped silently.
FORECAST_INTERVAL = 5.0
# One forecast is a short CLI call; a hung one must not hold the generation
# stream for the whole bridge timeout.
FORECAST_TIMEOUT = 30
# ut-agent reads the model key from this variable when ``--api-key-env`` is not
# given; passing the configured key this way keeps it out of the command line.
WITNESS_API_KEY_ENV = "UT_AGENT_JEV_API_KEY"


class BridgeError(RuntimeError):
    """ut-agent could not serve the request (configuration, crash, timeout)."""


@dataclass(frozen=True)
class BridgeSettings:
    root: str
    python: str
    extractor: str
    timeout: int
    generate_timeout: int
    execute_timeout: int
    # Seconds between budget-forecast observations of a running generation;
    # 0 (or less) turns the observation off.  Observation only: the verdict is
    # never fed back into generation.
    forecast_interval: float = FORECAST_INTERVAL

    @classmethod
    def from_config(cls, config) -> "BridgeSettings":
        return cls(
            root=config.get("UT_AGENT_ROOT", ""),
            python=config.get("UT_AGENT_PYTHON", ""),
            extractor=config.get("UT_AGENT_EXTRACTOR", ""),
            timeout=int(config.get("UT_BRIDGE_TIMEOUT", 120)),
            generate_timeout=int(config.get("UT_GENERATE_TIMEOUT", 3600)),
            execute_timeout=int(config.get("UT_EXECUTE_TIMEOUT", 300)),
            forecast_interval=float(config.get("UT_FORECAST_INTERVAL", FORECAST_INTERVAL)),
        )


class UtAgentBridge:
    def __init__(self, settings: BridgeSettings):
        self.settings = settings

    def _run(self, args: list[str], *, timeout: int,
             on_unit: Callable[[dict], None] | None = None,
             forecast: Callable[[], dict] | None = None,
             on_forecast: Callable[[dict], None] | None = None) -> dict[str, Any]:
        root, python = self.settings.root, self.settings.python
        if not root or not python:
            raise BridgeError("ut-agent 未配置：请设置 UT_AGENT_ROOT 与 UT_AGENT_PYTHON")
        env = os.environ.copy()
        # The checkout's sources win over any installed ut_agent distribution.
        env["PYTHONPATH"] = os.pathsep.join(
            part for part in (str(Path(root) / "src"), env.get("PYTHONPATH", "")) if part)
        env["PYTHONIOENCODING"] = "utf-8"
        command = [python, "-m", "ut_agent", "bridge", *args]
        try:
            if on_unit is None:
                # subprocess.run(timeout=) kills only the direct child. On
                # Windows the grandchildren keep the captured pipes open, so
                # the timeout wait itself never returns. Own the process and
                # kill the tree before draining.
                proc = self._wait(self._popen(command, root, env), command, timeout)
            else:
                directory = Path(args[args.index("--unit-ready-dir") + 1])
                output = Path(args[args.index("--out") + 1])
                proc = self._stream_units(command, root, env, timeout, directory, output, on_unit,
                                          forecast=forecast, on_forecast=on_forecast,
                                          interval=self.settings.forecast_interval)
        except subprocess.TimeoutExpired as exc:
            raise BridgeError(f"ut-agent bridge {args[0]} 超时（{timeout}s）") from exc
        except OSError as exc:
            raise BridgeError(f"无法启动 ut-agent：{exc}") from exc
        lines = proc.stdout.decode("utf-8", "replace").strip().splitlines()
        stderr_tail = proc.stderr.decode("utf-8", "replace")[-800:]
        try:
            document = json.loads(lines[-1]) if lines else None
        except ValueError:
            document = None
        if not isinstance(document, dict) or document.get("contract") != CONTRACT:
            raise BridgeError(
                f"ut-agent bridge {args[0]} 没有返回 {CONTRACT} 文档"
                f"（exit={proc.returncode}）：{stderr_tail}")
        if not document.get("ok"):
            error = document.get("error") or {}
            raise BridgeError(f"{error.get('type', 'Error')}: {error.get('message', '')}")
        return document

    @staticmethod
    def _popen(command, root, env) -> subprocess.Popen:
        return popen_owned(
            command, cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    @staticmethod
    def _kill_tree(child: subprocess.Popen) -> None:
        kill_process_tree(child.pid, getattr(child, "_owned_job", None))

    @staticmethod
    def _cleanup(child, *, drain=False):
        failures = []
        try:
            UtAgentBridge._kill_tree(child)
        except Exception as exc:
            failures.append(exc)
            for action in (child.kill, lambda: child.wait(timeout=2)):
                try:
                    action()
                except Exception as fallback:
                    failures.append(fallback)
        if drain:
            try:
                child.communicate(timeout=2)
            except Exception as exc:
                failures.append(exc)
        if failures:
            retain_cleanup(child, lambda: UtAgentBridge._cleanup(child, drain=True))
            raise BridgeError(
                "ut-agent cleanup could not drain or confirm termination; ownership retained"
            ) from failures[0]
        release_cleanup(child)

    def _wait(self, child: subprocess.Popen, command, timeout: int) -> subprocess.CompletedProcess:
        try:
            stdout, stderr = child.communicate(timeout=timeout)
        except BaseException as failure:
            try:
                self._cleanup(child, drain=True)
            except BridgeError as cleanup:
                raise BridgeError(f"{failure}; {cleanup}") from failure
            raise
        self._cleanup(child)
        return subprocess.CompletedProcess(command, child.returncode, stdout, stderr)

    @staticmethod
    def _stream_units(command, root, env, timeout, directory, output, on_unit, *,
                      forecast=None, on_forecast=None, interval=FORECAST_INTERVAL):
        from .generated_unit import read_receipt

        seen = set()
        deadline = time.monotonic() + timeout
        next_forecast = time.monotonic() + interval

        def consume():
            for path in sorted(directory.glob("*.json")):
                if path.name in seen:
                    continue
                try:
                    receipt = read_receipt(path, output)
                except (OSError, ValueError, TypeError) as exc:
                    raise BridgeError(f"Invalid completed-unit receipt {path.name}: {exc}") from exc
                on_unit(receipt)
                seen.add(path.name)

        def observe_forecast():
            # Budget observation rides the same poll tick as unit pickup.  It is
            # advisory by construction: on_forecast only records the verdict, and
            # any failure here must never disturb generation.
            nonlocal next_forecast
            now = time.monotonic()
            if forecast is None or interval <= 0 or now < next_forecast:
                return
            next_forecast = now + interval
            try:
                report = forecast()
                on_forecast(report)
            except (BridgeError, OSError, ValueError, TypeError,
                    subprocess.SubprocessError):
                return

        child = UtAgentBridge._popen(command, root, env)
        try:
            while True:
                consume()
                observe_forecast()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    stdout, stderr = child.communicate(timeout=min(0.1, remaining))
                except subprocess.TimeoutExpired:
                    continue
                consume()
                observe_forecast()
                break
        except BaseException as failure:
            # Pool workers inherit the output pipes. Killing only their
            # parent can leave communicate() waiting for live descendants.
            try:
                UtAgentBridge._cleanup(child, drain=True)
            except BridgeError as cleanup:
                raise BridgeError(f"{failure}; {cleanup}") from failure
            raise
        UtAgentBridge._cleanup(child)
        return subprocess.CompletedProcess(command, child.returncode, stdout, stderr)

    @staticmethod
    def _manifest(manifest: str) -> list[str]:
        return ["--manifest", manifest]

    def index(self, manifest: str, index_csv: str, product_root: str) -> list[dict[str, Any]]:
        document = self._run(["index", *self._manifest(manifest), "--index-csv", index_csv,
                              "--product-root", product_root], timeout=self.settings.timeout)
        return document["targets"]

    def constraints(self, manifest: str) -> dict[str, Any]:
        document = self._run(["constraints", *self._manifest(manifest)],
                             timeout=self.settings.timeout)
        return {key: value for key, value in document.items() if key not in ("contract", "ok")}

    def configuration_options(self) -> dict[str, Any]:
        return self._run(["configuration-options"],
                         timeout=self.settings.timeout)["configuration_options"]

    def forecast(self, snapshots_dir: Path, history_dir: Path | None = None) -> dict[str, Any]:
        """Read-only budget forecast of every function currently in ``snapshots_dir``."""
        args = ["forecast", "--snapshots-dir", str(snapshots_dir)]
        if history_dir is not None:
            args += ["--history-dir", str(history_dir)]
        return self._run(args, timeout=min(FORECAST_TIMEOUT, self.settings.timeout))["forecast"]

    def _forecast_probe(self, snapshots_dir: Path, history_dir: Path | None):
        """Poller for a running batch, or ``None`` when observation is off."""
        if self.settings.forecast_interval <= 0:
            return None
        return lambda: self.forecast(snapshots_dir, history_dir)

    def generate(self, manifest: str, index_csv: str, product_root: str, out: Path,
                 functions: list[str], *, progress_dir: Path,
                 on_unit: Callable[[dict], None] | None = None,
                 on_forecast: Callable[[dict], None] | None = None,
                 history_dir: Path | None = None,
                 function_step_limit: int | None = None,
                 budget_stop_observations: int | None = None,
                 stop_file: str | None = None) -> dict[str, Any]:
        if function_step_limit is not None and (
                type(function_step_limit) is not int or function_step_limit < 1):
            raise BridgeError("function_step_limit must be a positive integer")
        if budget_stop_observations is not None and (
                type(budget_stop_observations) is not int
                or not 1 <= budget_stop_observations <= 10):
            raise BridgeError("budget_stop_observations must be an integer from 1 to 10")
        args = ["generate", *self._manifest(manifest), "--index-csv", index_csv,
                "--product-root", product_root, "--out", str(out),
                "--progress-dir", str(progress_dir)]
        # Omitted, never defaulted here: ut-agent keeps its own ceiling, and its
        # forecast only ends a function when the batch asks for it.
        if function_step_limit is not None:
            args += ["--function-step-limit", str(function_step_limit)]
        if budget_stop_observations is not None:
            args += ["--budget-stop-observations", str(budget_stop_observations)]
        # Always passed: an operator may ask to stop at any point in the run, and
        # ut-agent only reads the file between obligations.
        if stop_file is not None:
            args += ["--stop-file", stop_file]
        for name in functions:
            args += ["--function", name]
        if self.settings.extractor:
            args += ["--clang-extractor", self.settings.extractor]
        if on_unit is not None:
            directory = out / "unit-ready"
            if directory.exists() and any(directory.iterdir()):
                raise BridgeError("Completed-unit directory is not empty; refusing stale generation receipts")
            args += ["--unit-ready-dir", str(directory)]
            return self._run(args, timeout=self.settings.generate_timeout, on_unit=on_unit,
                             forecast=self._forecast_probe(progress_dir, history_dir),
                             on_forecast=on_forecast)
        return self._run(args, timeout=self.settings.generate_timeout)

    # ut-agent's own proposal default; triage is deterministic and needs far less.
    WITNESS_TIMEOUT = 900
    # One confirmation compiles and runs a single-case harness per candidate, so
    # it gets more than a plain bridge call and less than a generation.
    WITNESS_CONFIRM_TIMEOUT = 120

    def _witness(self, args: list[str], *, timeout: int,
                 api_key: str | None = None) -> dict[str, Any]:
        """Run one ``ut-agent witness`` command and return its JSON document.

        Witness is a top-level command, not a bridge one: it prints its document
        without a contract key, and reports a failed run as exit code 2 plus a
        message on stderr.  The exit code therefore decides failure; a document
        is only read from a run that exited zero.

        The API key travels in ut-agent's own environment variable, never argv:
        a command line is readable by other processes on the same host.
        """
        root, python = self.settings.root, self.settings.python
        if not root or not python:
            raise BridgeError("ut-agent 未配置：请设置 UT_AGENT_ROOT 与 UT_AGENT_PYTHON")
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            part for part in (str(Path(root) / "src"), env.get("PYTHONPATH", "")) if part)
        env["PYTHONIOENCODING"] = "utf-8"
        if api_key:
            env[WITNESS_API_KEY_ENV] = api_key
        command = [python, "-m", "ut_agent", "witness", *args]
        try:
            proc = self._wait(self._popen(command, root, env), command, timeout)
        except subprocess.TimeoutExpired as exc:
            raise BridgeError(f"ut-agent witness {args[0]} 超时（{timeout}s）") from exc
        except OSError as exc:
            raise BridgeError(f"无法启动 ut-agent：{exc}") from exc
        lines = proc.stdout.decode("utf-8", "replace").strip().splitlines()
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        try:
            document = json.loads(lines[-1]) if lines else None
        except ValueError:
            document = None
        if proc.returncode != 0:
            reason = stderr.splitlines()[-1] if stderr else (lines[-1] if lines else "")
            raise BridgeError(
                f"ut-agent witness {args[0]} 失败（exit={proc.returncode}）：{reason}")
        if not isinstance(document, dict):
            raise BridgeError(
                f"ut-agent witness {args[0]} 没有返回 JSON 文档（exit={proc.returncode}）")
        return document

    def triage(self, *, manifest: str, ir: str, suite: str, out: Path,
               batch_size: int = 8, timeout: int | None = None) -> dict[str, Any]:
        """Sort this function's unsolved obligations into proposable or not.

        Reads the generation inputs only; writes ``out``, which must not exist.
        """
        return self._witness(
            ["triage", *self._manifest(manifest), "--ir", str(ir), "--suite", str(suite),
             "--out", str(out), "--batch-size", str(batch_size)],
            timeout=timeout or self.WITNESS_TIMEOUT)

    def propose(self, *, manifest: str, ir: str, suite: str, out: Path,
                base_url: str, model: str, batch_size: int = 8,
                max_tokens: int | None = None,
                api_key: str | None = None,
                timeout: int | None = None) -> dict[str, Any]:
        """Ask the operator's local model for candidate inputs from a triage.

        Only an explicit operator action reaches this method; nothing in the
        batch lifecycle calls it.
        """
        args = ["propose", *self._manifest(manifest), "--ir", str(ir), "--suite", str(suite),
                "--out", str(out), "--base-url", base_url, "--model", model,
                "--batch-size", str(batch_size)]
        if max_tokens is not None:
            args += ["--max-tokens", str(max_tokens)]
        return self._witness(args, timeout=timeout or self.WITNESS_TIMEOUT, api_key=api_key)

    def confirm(self, *, manifest: str, ir: str, candidates: str, out: Path,
                clang: str, llvm_cov: str, llvm_profdata: str,
                profile_runtime: str = "", write: bool = False,
                timeout: float | None = None) -> dict[str, Any]:
        """Replay candidates through the FunctionIR matcher and a host harness.

        The caller decides whether ``--write`` is allowed; with it, ut-agent
        merges only the confirmed entries into the ledger the manifest declares.
        """
        args = ["confirm", *self._manifest(manifest), "--ir", str(ir),
                "--candidates", str(candidates), "--out", str(out),
                "--clang", clang, "--llvm-cov", llvm_cov, "--llvm-profdata", llvm_profdata,
                "--timeout", str(timeout or self.WITNESS_CONFIRM_TIMEOUT)]
        if profile_runtime:
            args += ["--profile-runtime", str(profile_runtime)]
        if write:
            args += ["--write"]
        return self._witness(args, timeout=int(timeout or self.WITNESS_CONFIRM_TIMEOUT))

    @staticmethod
    def _host_args(ir_json: Path, intents: Path, reference: Path,
                   candidate: Path, out: Path) -> list[str]:
        return ["--ir-json", str(ir_json), "--intents", str(intents),
                "--reference", str(reference), "--candidate", str(candidate), "--out", str(out)]

    def host_prepare(self, *, ir_json: Path, intents: Path, reference: Path,
                     candidate: Path, out: Path, manifest: Path | None = None) -> dict[str, Any]:
        args = ["host-prepare", *self._host_args(ir_json, intents, reference, candidate, out)]
        if manifest is not None:
            args += ["--manifest", str(manifest)]
        return self._run(args, timeout=self.settings.timeout)["prepared"]

    def execute(self, *, ir_json: Path, intents: Path, reference: Path,
                     candidate: Path, out: Path, config: dict,
                     stub_request: Optional[Path] = None,
                     stub_response: Optional[Path] = None) -> dict[str, Any]:
        args = ["execute", *self._host_args(ir_json, intents, reference, candidate, out),
                "--timeout", str(self.settings.execute_timeout)]
        if config.get("manifest_path"):
            args += ["--manifest", str(config["manifest_path"])]
        for flag, value in (("--clang", config.get("host_clang", "clang")),
                            ("--llvm-cov", config.get("host_llvm_cov", "llvm-cov")),
                            ("--llvm-profdata", config.get("host_llvm_profdata", "llvm-profdata")),
                            ("--profile-runtime", config.get("host_profile_runtime")),
                            ("--stub-request", stub_request), ("--stub-response", stub_response)):
            if value:
                args += [flag, str(value)]
        document = self._run(args, timeout=3 * self.settings.execute_timeout + 120)
        if not isinstance(document.get("execution"), dict) or not isinstance(document.get("check"), dict):
            raise BridgeError("LLVM execute returned no execution/check evidence")
        return {"execution": document["execution"], "check": document["check"]}

    def host_execute(self, **kwargs):
        """Alias used by older call sites; the subprocess command remains execute."""
        return self.execute(**kwargs)

    def check(self, candidate: Path, *, manifest: str, reference: Optional[Path] = None,
              previous: Optional[Path] = None, intents: Optional[Path] = None,
              ir_json: Optional[Path] = None) -> dict[str, Any]:
        args = ["check", *self._manifest(manifest), "--candidate", str(candidate)]
        for flag, value in (("--reference", reference), ("--previous", previous),
                            ("--intents", intents), ("--ir-json", ir_json)):
            if value is not None:
                args += [flag, str(value)]
        return self._run(args, timeout=self.settings.timeout)["check"]
