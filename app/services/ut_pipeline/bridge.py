"""Subprocess client for ``python -m ut_agent bridge`` (contract ``ut-agent.bridge/1``).

ut-agent runs in its own interpreter and checkout; this platform never imports
it.  Each call prints one JSON document; ``ok: false`` or a missing/foreign
contract raises :class:`BridgeError` so callers can route the job to a human.
"""

from __future__ import annotations

import json
import os
import subprocess
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
    winams: str
    timeout: int
    generate_timeout: int
    execute_timeout: int
    omf_converter: str = ""

    @classmethod
    def from_config(cls, config) -> "BridgeSettings":
        return cls(
            root=config.get("UT_AGENT_ROOT", ""),
            python=config.get("UT_AGENT_PYTHON", ""),
            extractor=config.get("UT_AGENT_EXTRACTOR", ""),
            winams=config.get("WINAMS_EXECUTABLE", ""),
            timeout=int(config.get("UT_BRIDGE_TIMEOUT", 120)),
            generate_timeout=int(config.get("UT_GENERATE_TIMEOUT", 3600)),
            execute_timeout=int(config.get("UT_EXECUTE_TIMEOUT", 300)),
            omf_converter=config.get("UT_OMF_CONVERTER", ""),
        )


class UtAgentBridge:
    def __init__(self, settings: BridgeSettings):
        self.settings = settings

    def _run(self, args: list[str], *, timeout: int) -> dict[str, Any]:
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
            proc = subprocess.run(command, cwd=root, env=env, capture_output=True,
                                  timeout=timeout, check=False)
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
                 functions: list[str]) -> dict[str, Any]:
        args = ["generate", *self._manifest(manifest), "--index-csv", index_csv,
                "--product-root", product_root, "--out", str(out)]
        for name in functions:
            args += ["--function", name]
        if self.settings.extractor:
            args += ["--clang-extractor", self.settings.extractor]
        return self._run(args, timeout=self.settings.generate_timeout)

    def execute(self, unit_dir: Path, candidate: Path, out: Path,
                stub_builds: Optional[list[str]] = None,
                source_rel: str = "") -> dict[str, Any]:
        args = ["execute", "--unit-dir", str(unit_dir), "--candidate", str(candidate),
                "--out", str(out), "--timeout", str(self.settings.execute_timeout)]
        if self.settings.winams:
            args += ["--winams", self.settings.winams]
        # ut-agent picks the first build whose map defines every declared stub
        # and whose MCDC build instruments the source under test.
        for build in stub_builds or ():
            args += ["--stub-build", build]
        if stub_builds and source_rel:
            args += ["--source-rel", source_rel]
        if self.settings.omf_converter:
            args += ["--omf-converter", self.settings.omf_converter]
        # WinAMS gets its own timeout; leave headroom for sandbox copy and parsing.
        return self._run(args, timeout=self.settings.execute_timeout + 120)["execution"]

    def check(self, candidate: Path, *, manifest: str, reference: Optional[Path] = None,
              previous: Optional[Path] = None, execution: Optional[Path] = None,
              intents: Optional[Path] = None) -> dict[str, Any]:
        args = ["check", *self._manifest(manifest), "--candidate", str(candidate)]
        for flag, value in (("--reference", reference), ("--previous", previous),
                            ("--execution", execution), ("--intents", intents)):
            if value is not None:
                args += [flag, str(value)]
        return self._run(args, timeout=self.settings.timeout)["check"]
