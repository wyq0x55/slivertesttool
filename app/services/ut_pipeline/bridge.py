"""Subprocess client for ``python -m ut_agent bridge`` (contract ``ut-agent.bridge/1``).

ut-agent runs in its own interpreter and checkout; this platform never imports
it.  Each call prints one JSON document; ``ok: false`` or a missing/foreign
contract raises :class:`BridgeError` so callers can route the job to a human.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

CONTRACT = "ut-agent.bridge/1"


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

    @classmethod
    def from_config(cls, config) -> "BridgeSettings":
        return cls(
            root=config.get("UT_AGENT_ROOT", ""),
            python=config.get("UT_AGENT_PYTHON", ""),
            extractor=config.get("UT_AGENT_EXTRACTOR", ""),
            timeout=int(config.get("UT_BRIDGE_TIMEOUT", 120)),
            generate_timeout=int(config.get("UT_GENERATE_TIMEOUT", 3600)),
            execute_timeout=int(config.get("UT_EXECUTE_TIMEOUT", 300)),
        )


class UtAgentBridge:
    def __init__(self, settings: BridgeSettings):
        self.settings = settings

    def _run(self, args: list[str], *, timeout: int,
             on_unit: Callable[[dict], None] | None = None) -> dict[str, Any]:
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
                proc = subprocess.run(command, cwd=root, env=env, capture_output=True,
                                      timeout=timeout, check=False)
            else:
                directory = Path(args[args.index("--unit-ready-dir") + 1])
                output = Path(args[args.index("--out") + 1])
                proc = self._stream_units(command, root, env, timeout, directory, output, on_unit)
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
    def _stream_units(command, root, env, timeout, directory, output, on_unit):
        from .generated_unit import read_receipt

        seen = set()
        deadline = time.monotonic() + timeout

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

        child = subprocess.Popen(command, cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 start_new_session=os.name != "nt")
        try:
            while True:
                consume()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    stdout, stderr = child.communicate(timeout=min(0.1, remaining))
                except subprocess.TimeoutExpired:
                    continue
                consume()
                return subprocess.CompletedProcess(command, child.returncode, stdout, stderr)
        except BaseException as failure:
            # Pool workers inherit the output pipes. Killing only their
            # parent can leave communicate() waiting for live descendants.
            if child.poll() is None:
                if os.name == "nt":
                    try:
                        subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                                       capture_output=True, check=False, timeout=5)
                    except (OSError, subprocess.TimeoutExpired):
                        child.kill()
                else:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            child.kill()
            try:
                child.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                # Do not enter Popen.__exit__ or close reader-owned pipes here:
                # either can wait indefinitely when OS tree termination fails.
                raise BridgeError(f"{failure}; generation cleanup could not drain descendant pipes; "
                                  "check remaining generation processes") from failure
            raise

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

    def generate(self, manifest: str, index_csv: str, product_root: str, out: Path,
                 functions: list[str], *, progress_dir: Path,
                 on_unit: Callable[[dict], None] | None = None) -> dict[str, Any]:
        args = ["generate", *self._manifest(manifest), "--index-csv", index_csv,
                "--product-root", product_root, "--out", str(out),
                "--progress-dir", str(progress_dir)]
        for name in functions:
            args += ["--function", name]
        if self.settings.extractor:
            args += ["--clang-extractor", self.settings.extractor]
        if on_unit is not None:
            directory = out / "unit-ready"
            if directory.exists() and any(directory.iterdir()):
                raise BridgeError("Completed-unit directory is not empty; refusing stale generation receipts")
            args += ["--unit-ready-dir", str(directory)]
            return self._run(args, timeout=self.settings.generate_timeout, on_unit=on_unit)
        return self._run(args, timeout=self.settings.generate_timeout)


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
              previous: Optional[Path] = None, execution: Optional[Path] = None,
              intents: Optional[Path] = None, ir_json: Optional[Path] = None) -> dict[str, Any]:
        args = ["check", *self._manifest(manifest), "--candidate", str(candidate)]
        for flag, value in (("--reference", reference), ("--previous", previous),
                            ("--execution", execution), ("--intents", intents), ("--ir-json", ir_json)):
            if value is not None:
                args += [flag, str(value)]
        return self._run(args, timeout=self.settings.timeout)["check"]
