"""The `ut-agent witness` wrapper: argv, environment and failure translation.

Witness is a top-level command that prints its document without a contract key
and reports failure as a non-zero exit, so the wrapper owns both the command
shape and the translation into BridgeError.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.ut_pipeline.bridge import (
    BridgeError, BridgeSettings, UtAgentBridge, WITNESS_API_KEY_ENV,
)


def _bridge(tmp_path):
    settings = BridgeSettings(root=str(tmp_path), python="py", extractor="",
                              timeout=5, generate_timeout=5, execute_timeout=5)
    return UtAgentBridge(settings)


def _completion(bridge, monkeypatch, *, returncode=0, stdout=b"", stderr=b""):
    seen = {}

    def wait(self, child, command, timeout):
        seen["command"] = command
        seen["timeout"] = timeout
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    def popen(command, root, env):
        seen["env"] = env
        return None

    monkeypatch.setattr(UtAgentBridge, "_popen", staticmethod(popen))
    monkeypatch.setattr(UtAgentBridge, "_wait", wait)
    seen["bridge"] = bridge
    return seen


def test_triage_argv_names_the_declared_inputs(tmp_path, monkeypatch):
    seen = _completion(
        _bridge(tmp_path), monkeypatch,
        stdout=b'progress line\n{"out": "o", "status": "PROPOSABLE"}\n')

    document = _bridge(tmp_path).triage(
        manifest="m.json", ir="i.json", suite="s.json", out=tmp_path / "o")

    assert document == {"out": "o", "status": "PROPOSABLE"}
    assert seen["command"][:5] == ["py", "-m", "ut_agent", "witness", "triage"]
    assert seen["command"][5:] == ["--manifest", "m.json", "--ir", "i.json",
                                   "--suite", "s.json", "--out", str(tmp_path / "o"),
                                   "--batch-size", "8"]
    # The tool keeps its own rules; silver never points it at another root.
    assert "--config-root" not in seen["command"]
    assert seen["timeout"] == UtAgentBridge.WITNESS_TIMEOUT


def test_triage_forwards_a_batch_size(tmp_path, monkeypatch):
    seen = _completion(_bridge(tmp_path), monkeypatch, stdout=b'{"status": "PROPOSABLE"}')

    _bridge(tmp_path).triage(manifest="m", ir="i", suite="s", out="o", batch_size=3)

    assert seen["command"][-1] == "3"


def test_propose_argv_carries_the_model_and_the_limit(tmp_path, monkeypatch):
    seen = _completion(_bridge(tmp_path), monkeypatch, stdout=b'{"status": "PROPOSABLE"}')

    _bridge(tmp_path).propose(
        manifest="m", ir="i", suite="s", out="o",
        base_url="http://127.0.0.1:3425/v1", model="qwen", max_tokens=512)

    command = seen["command"]
    assert command[:5] == ["py", "-m", "ut_agent", "witness", "propose"]
    assert command[command.index("--base-url") + 1] == "http://127.0.0.1:3425/v1"
    assert command[command.index("--model") + 1] == "qwen"
    assert command[command.index("--max-tokens") + 1] == "512"
    assert "--config-root" not in command and "--write" not in command


def test_propose_omits_the_token_limit_when_undeclared(tmp_path, monkeypatch):
    seen = _completion(_bridge(tmp_path), monkeypatch, stdout=b'{"status": "PROPOSABLE"}')

    _bridge(tmp_path).propose(manifest="m", ir="i", suite="s", out="o",
                              base_url="http://127.0.0.1:1/v1", model="qwen")

    assert "--max-tokens" not in seen["command"]


def test_a_proposal_carries_the_key_in_the_environment_not_argv(tmp_path, monkeypatch):
    monkeypatch.setenv(WITNESS_API_KEY_ENV, "not-the-key")
    seen = _completion(_bridge(tmp_path), monkeypatch, stdout=b'{"status": "PROPOSABLE"}')

    _bridge(tmp_path).propose(manifest="m", ir="i", suite="s", out="o",
                              base_url="http://127.0.0.1:1/v1", model="qwen",
                              api_key="sk-local-secret")

    assert seen["env"][WITNESS_API_KEY_ENV] == "sk-local-secret"
    assert "sk-local-secret" not in " ".join(seen["command"])


def test_a_triage_run_does_not_touch_the_model_key(tmp_path, monkeypatch):
    monkeypatch.setenv(WITNESS_API_KEY_ENV, "inherited")
    seen = _completion(_bridge(tmp_path), monkeypatch, stdout=b'{"status": "PROPOSABLE"}')

    _bridge(tmp_path).triage(manifest="m", ir="i", suite="s", out="o")

    # The tool's own inherited value is left alone: triage calls no model.
    assert seen["env"][WITNESS_API_KEY_ENV] == "inherited"


def test_a_timeout_names_the_command(tmp_path, monkeypatch):
    monkeypatch.setattr(UtAgentBridge, "_popen",
                        staticmethod(lambda command, root, env: None))

    def wait(self, child, command, timeout):
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(UtAgentBridge, "_wait", wait)

    with pytest.raises(BridgeError) as error:
        _bridge(tmp_path).triage(manifest="m", ir="i", suite="s", out="o")
    assert "witness triage 超时（900s）" in str(error.value)


def test_a_failed_run_reports_the_last_stderr_line(tmp_path, monkeypatch):
    _completion(_bridge(tmp_path), monkeypatch, returncode=2,
                stdout=b'{"status": "PROPOSABLE"}',
                stderr="noise\n[witness/triage] suite 与函数不一致".encode())

    with pytest.raises(BridgeError) as error:
        _bridge(tmp_path).triage(manifest="m", ir="i", suite="s", out="o")
    message = str(error.value)
    assert "失败（exit=2）" in message and "suite 与函数不一致" in message
    assert "noise" not in message


def test_a_run_without_a_document_is_an_error(tmp_path, monkeypatch):
    _completion(_bridge(tmp_path), monkeypatch, stdout=b"not json at all")

    with pytest.raises(BridgeError) as error:
        _bridge(tmp_path).triage(manifest="m", ir="i", suite="s", out="o")
    assert "没有返回 JSON 文档" in str(error.value)


def test_a_missing_ut_agent_is_reported(tmp_path):
    bridge = UtAgentBridge(BridgeSettings(root="", python="", extractor="",
                                          timeout=5, generate_timeout=5, execute_timeout=5))

    with pytest.raises(BridgeError, match="ut-agent 未配置"):
        bridge.triage(manifest="m", ir="i", suite="s", out="o")