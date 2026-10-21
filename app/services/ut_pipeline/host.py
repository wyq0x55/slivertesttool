"""Experimental host execution and bounded, non-authoritative stub proposals.

Only ut-agent owns typed facts, harness construction, CSV binding and compilation.
This service owns the model call, durable artifacts and workflow checkpoints.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from ..ai import config as ai_config
from ..ai import provider
from .bridge import BridgeError

_EVIDENCE_IDS = {"typed-function-ir-v5", "suite-stub-projection-v1"}
_PROPOSAL_KEYS = {"status", "stub_bodies", "evidence_ids", "limitations"}
_STATUSES = {"CANDIDATE", "UNKNOWN", "UNSUPPORTED", "NEEDS_REVIEW"}


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_proposal(reply: str) -> dict:
    """Validate the envelope shape; C/body validity belongs to host-execute."""
    proposal = json.loads(reply)
    if not isinstance(proposal, dict) or set(proposal) != _PROPOSAL_KEYS:
        raise ValueError("Expected exactly status, stub_bodies, evidence_ids and limitations")
    if not isinstance(proposal["status"], str) or proposal["status"] not in _STATUSES:
        raise ValueError("Unknown stub proposal status")
    bodies = proposal["stub_bodies"]
    if not isinstance(bodies, dict) or any(
            not isinstance(k, str) or not k or not isinstance(v, str) for k, v in bodies.items()):
        raise ValueError("stub_bodies must map callee names to body statements")
    ids = proposal["evidence_ids"]
    if not isinstance(ids, list) or any(not isinstance(x, str) for x in ids):
        raise ValueError("evidence_ids must be a list of strings")
    if not isinstance(proposal["limitations"], list) or any(
            not isinstance(x, str) for x in proposal["limitations"]):
        raise ValueError("limitations must be a list of strings")
    if proposal["status"] == "CANDIDATE":
        if len(ids) != 2 or set(ids) != _EVIDENCE_IDS:
            raise ValueError("Candidate must cite the typed IR and suite stub projection")
    elif bodies or not proposal["limitations"] or not any(x.strip() for x in proposal["limitations"]):
        raise ValueError("An abstention requires empty stub_bodies and nonempty limitations")
    return proposal


def _mismatch_cases(execution: dict) -> list:
    """Keep actual observations in repair feedback without an unbounded trace."""
    result = []
    reference = {case.get("case_id"): case for case in
                 (execution.get("reference_execution") or {}).get("cases", ()) if isinstance(case, dict)}
    for case in execution.get("cases") or ():
        if not isinstance(case, dict):
            continue
        cells = case.get("cells") or case.get("observations") or []
        if isinstance(cells, list):
            mismatches = [cell for cell in cells if isinstance(cell, dict) and (
                cell.get("match") is False or cell.get("matched") is False or
                cell.get("status") in ("MISMATCH", "FAIL") or
                ("expected" in cell and "observed" in cell and cell["expected"] != cell["observed"]) or
                ("expected" in cell and "actual" in cell and cell["expected"] != cell["actual"]))]
        else:
            mismatches = []
        observations = case.get("observations") or {}
        baseline = (reference.get(case.get("case_id")) or {}).get("observations") or {}
        differences = [{"key": key, "observed": value, "reference": baseline.get(key)}
                       for key, value in observations.items() if key in baseline and value != baseline[key]][:10]
        if mismatches or differences or case.get("status") in ("HOST_MISMATCH", "MISMATCH", "FAIL"):
            result.append({"case_id": case.get("case_id"), "status": case.get("status"),
                           "cells": mismatches[:10], "observations": differences, "reason": case.get("reason")})
        if len(result) == 10:
            break
    return result


def execute(*, bridge, ir_json, intents, reference, candidate, out: Path, config: dict,
            model_timeout: int, persist, continuing) -> tuple[dict, dict]:
    out.mkdir(parents=True, exist_ok=False)
    metadata = {"mode": config.get("stub_mode", "deterministic"), "rounds": [],
                "artifact_root": str(out.resolve()), "artifacts": []}
    latest = {"status": "HOST_UNSUPPORTED", "evidence_source": "HOST_EXECUTION",
              "counts_as_target_evidence": False, "delivery_validated": False}

    def save():
        artifacts = []
        for path in sorted(out.rglob("*")):
            if (not path.is_file() or path.is_symlink() or
                    not path.resolve().is_relative_to(out.resolve()) or
                    path.suffix.lower() not in {".json", ".txt", ".c", ".h", ".ll"}):
                continue
            relative = path.relative_to(out).as_posix()
            artifacts.append({"id": hashlib.sha256(relative.encode()).hexdigest()[:16],
                              "name": relative, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        metadata["artifacts"] = artifacts
        latest["stub_generation"] = copy.deepcopy(metadata)
        persist(copy.deepcopy(latest))

    def blocked(reason, code="HOST_UNSUPPORTED"):
        latest.update(reason=reason)
        check = {"verdict": "BLOCKED", "executed": False, "repairable": False,
                 "evidence_source": "HOST_EXECUTION", "counts_as_target_evidence": False,
                 "delivery_validated": False, "issues": [
                     {"code": code, "severity": "error", "repairable": False, "message": reason}]}
        _write_json(out / "execution.json", latest)
        _write_json(out / "check.json", check)
        save()
        return latest, check

    inputs = {"ir_json": ir_json, "intents": intents, "reference": reference, "candidate": candidate}
    if any(not value or not Path(value).is_file() for value in inputs.values()):
        return blocked("Generator IR, intents, reference and candidate files are required; regenerate this job.")
    arguments = {key: Path(value) for key, value in inputs.items()}

    def run(directory, request=None, response=None):
        nonlocal latest
        if not continuing():
            return blocked("Stopped before host execution", "STOPPED")
        result = bridge.host_execute(**arguments, out=directory, config=config,
                                     stub_request=request, stub_response=response)
        latest = {**result["execution"], "evidence_source": "HOST_EXECUTION",
                  "counts_as_target_evidence": False, "delivery_validated": False}
        check = {**result["check"], "evidence_source": "HOST_EXECUTION",
                 "counts_as_target_evidence": False, "delivery_validated": False, "repairable": False}
        if check.get("verdict") not in ("PASS", "FAIL", "BLOCKED"):
            raise BridgeError("host-execute returned an unknown verdict")
        save()
        return latest, check

    if not continuing():
        return blocked("Stopped before host execution", "STOPPED")
    if metadata["mode"] != "llm":
        return run(out / "host")

    prepared = bridge.host_prepare(**arguments, out=out / "prepare")
    _write_json(out / "prepared.json", prepared)
    metadata["prepared"] = prepared
    save()
    if prepared.get("status") != "READY":
        return blocked(prepared.get("reason") or "Host preparation is unsupported")
    if type(prepared.get("stub_count")) is not int or prepared["stub_count"] < 0:
        raise BridgeError("host-prepare returned no valid stub_count")
    if prepared["stub_count"] == 0:
        metadata["skipped"] = "No stubs; model was not contacted"
        return run(out / "host")
    request = Path(prepared.get("request_path") or "")
    if (not request.is_file() or not request.resolve().is_relative_to((out / "prepare").resolve())
            or hashlib.sha256(request.read_bytes()).hexdigest() != prepared.get("request_sha256")):
        raise BridgeError("host-prepare request path/digest does not match its artifact")
    messages = prepared.get("messages")
    if not isinstance(messages, list) or not messages or any(
            not isinstance(m, dict) or m.get("role") not in ("system", "user")
            or not isinstance(m.get("content"), str) for m in messages):
        raise BridgeError("host-prepare returned invalid model messages")
    messages = copy.deepcopy(messages)
    model = config.get("stub_model") or ai_config.get_ai_config()[ai_config.KEY_MODEL]
    metadata["model"] = model
    limit = config.get("stub_max_rounds", 2)
    if type(limit) is not int or not 1 <= limit <= 5:
        raise BridgeError("Invalid frozen stub_max_rounds")
    for number in range(1, limit + 1):
        if not continuing():
            return blocked("Stopped before the next stub generation round", "STOPPED")
        directory = out / f"round-{number}"
        directory.mkdir()
        _write_json(directory / "messages.json", messages)
        usage = {}
        record = {"round": number, "status": "REQUESTING", "model": model,
                  "prompt_path": f"round-{number}/messages.json",
                  "response_path": f"round-{number}/response.txt",
                  "prompt_sha256": hashlib.sha256(
                      json.dumps(messages, ensure_ascii=False).encode()).hexdigest(),
                  "request_sha256": prepared["request_sha256"], "usage": usage}
        metadata["rounds"].append(record)
        save()
        try:
            with provider.capture_provider_provenance() as receipt:
                reply = provider.invoke_chat(messages, model=model or None, temperature=0,
                                             max_tokens=8192, json_mode=True, usage=usage,
                                             timeout=model_timeout)
        except provider.ProviderError:
            record.update(status="MODEL_UNAVAILABLE", usage=dict(usage), provider_provenance=dict(receipt))
            save()
            raise
        record.update(usage=dict(usage), provider_provenance=dict(receipt))
        (directory / "response.txt").write_text(reply, encoding="utf-8")
        try:
            proposal = parse_proposal(reply)
        except (ValueError, TypeError) as exc:
            record.update(status="INVALID", reason=str(exc))
            diagnostic = str(exc)
        else:
            record["proposal_status"] = proposal["status"]
            envelope = {"request_sha256": prepared["request_sha256"], "proposal": proposal,
                        "model": model, "usage": dict(usage)}
            response = directory / "proposal.json"
            _write_json(response, envelope)
            record.update(status="VALIDATING", proposal_path=f"round-{number}/proposal.json")
            save()
            if not continuing():
                record["status"] = "STOPPED"
                return blocked("Stopped after model response; candidate retained", "STOPPED")
            if proposal["status"] != "CANDIDATE":
                record["status"] = proposal["status"]
                return blocked("Model returned " + proposal["status"] + ": " + "; ".join(proposal["limitations"]))
            try:
                execution, check = run(directory / "host", request, response)
            except BridgeError as exc:
                record.update(status="TOOL_ERROR", reason=str(exc))
                save()
                raise
            record.update(status=execution.get("status"), verdict=check["verdict"], reason=execution.get("reason"),
                          execution_path=f"round-{number}/host/execution.json",
                          check_path=f"round-{number}/host/check.json")
            save()
            retryable = execution.get("status") in (
                "HOST_COMPILE_ERROR", "HOST_RUNTIME_ERROR", "HOST_MISMATCH") or (
                execution.get("status") == "HOST_UNSUPPORTED" and
                execution.get("reason_code") == "HOST_STUB_BODY_INVALID")
            if check["verdict"] == "PASS" or not retryable:
                return execution, check
            diagnostic = json.dumps({"status": execution.get("status"), "reason": execution.get("reason"),
                                     "reason_code": execution.get("reason_code"),
                                     "reference_status": (execution.get("reference_execution") or {}).get("status"),
                                     "reference_reason": (execution.get("reference_execution") or {}).get("reason"),
                                     "cases": _mismatch_cases(execution),
                                     "build": execution.get("build"), "issues": check.get("issues")},
                                    ensure_ascii=False)[:16000]
            if number == limit:
                return execution, check
        save()
        messages = [*messages[:2], {"role": "assistant", "content": reply},
                    {"role": "user", "content": "Repair only the stub bodies using this machine feedback. "
                     "Return the same JSON schema.\n" + diagnostic}]
    return blocked("Stub generation rounds exhausted; no valid candidate", "STUB_CANDIDATE_INVALID")
