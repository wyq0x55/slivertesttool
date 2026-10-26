"""Missing prerequisites need actionable guidance, not a claim of impossibility."""

import pytest

from app.models import UtAttempt, UtJob
from app.models.ut_pipeline import GateCode
from app.services.ut_pipeline import diagnostic
from app.services.ut_pipeline.pipeline import next_state


def test_abi_block_remains_human_gated_and_exposes_configuration_steps():
    check = {"verdict": "BLOCKED", "repairable": False, "issues": [{
        "code": "ABI_POINTER_SIZE_UNDECLARED", "severity": "error", "repairable": False,
        "guidance": {"action_scope": "PROJECT_CONFIGURATION",
                     "steps": ["检查 build.profile", "重新提取并创建新批次"]},
    }]}
    state, reason, gate = next_state(check, UtAttempt.ORIGIN_GENERATOR, 0, 5)
    assert state == UtJob.NEEDS_HUMAN and gate == GateCode.CHECK_BLOCKED
    assert "ABI_POINTER_SIZE_UNDECLARED" in reason
    assert "build.profile" in reason and "新批次" in reason
    assert "无法自动修复" not in reason


def test_unrecognized_block_does_not_claim_the_problem_is_unfixable():
    check = {"verdict": "BLOCKED", "issues": [{
        "code": "NEW_BLOCK", "severity": "error",
    }]}
    state, reason, gate = next_state(check, UtAttempt.ORIGIN_GENERATOR, 0, 5)
    assert state == UtJob.NEEDS_HUMAN and gate == GateCode.CHECK_BLOCKED
    assert "NEW_BLOCK" in reason and "无法自动修复" not in reason


@pytest.mark.parametrize("instructions", [7, "build.profile", {"unsafe": "steps"}, None])
def test_malformed_optional_guidance_cannot_interrupt_the_human_gate(instructions):
    check = {"verdict": "BLOCKED", "issues": [{
        "code": "ABI_POINTER_SIZE_UNDECLARED", "severity": "error",
        "guidance": {"action_scope": "PROJECT_CONFIGURATION", "steps": instructions},
    }]}
    state, reason, gate = next_state(check, UtAttempt.ORIGIN_GENERATOR, 0, 5)
    assert state == UtJob.NEEDS_HUMAN and gate == GateCode.CHECK_BLOCKED
    assert "ABI_POINTER_SIZE_UNDECLARED" in reason and "处理步骤" not in reason


def test_projection_failure_preserves_location_without_requiring_a_contract():
    unit = {"status": "NEEDS_REVIEW", "error": "original typed diagnostic",
            "projection_failure": {"code": "MISSING_INTERFACE_CONTRACT",
                                   "callee": "external_read", "argument_index": 0}}
    reason = diagnostic.generation_failure_reason(unit)
    assert "external_read 的第 1 个参数（argument_index=0）" in reason
    assert "无需先填写接口契约或指针方向" in reason
    assert "自动 Stub 支持" in reason and "ut-agent" in reason
    assert "新批次" in reason and "original typed diagnostic" in reason
    assert "interfaces.entries" in reason and "interfaces.contract" in reason
    assert "须验证" in reason and "不证明真实接口语义" in reason
    assert "检查该调用的实现是否已纳入" not in reason


@pytest.mark.parametrize("failure", [
    {"code": "MISSING_INTERFACE_CONTRACT"},
    {"code": "MISSING_INTERFACE_CONTRACT", "callee": "dep", "argument_index": True},
    {"code": "MISSING_INTERFACE_CONTRACT", "callee": "dep", "argument_index": -1},
])
def test_contract_failure_without_an_exact_location_remains_actionable(failure):
    reason = diagnostic.generation_failure_reason({
        "status": "NEEDS_REVIEW", "projection_failure": failure,
    })
    assert "外部调用的指针参数" in reason
    assert "MISSING_INTERFACE_CONTRACT" in reason
    assert "无需先填写接口契约或指针方向" in reason
    assert "原始诊断" in reason


def test_other_generation_failures_keep_original_evidence():
    assert diagnostic.generation_failure_reason({
        "status": "NEEDS_REVIEW", "error": "original compiler diagnostic",
    }) == "生成器未产出 TestCsv（NEEDS_REVIEW）：original compiler diagnostic"
