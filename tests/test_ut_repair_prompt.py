"""Repair prompt layout: stable content first so the local model can reuse its prefix cache."""

from __future__ import annotations

from app.services.ut_pipeline import repair

CONSTRAINTS = {
    "baseline": {"id": "base", "version": "1", "source": {"document": "doc", "sheet": "s"}},
    "rules": [{"rule_id": "R1", "meaning": "rows follow the baseline", "source": "doc"}],
    "revision_rules": ["do not rename columns"],
    "policies": {"b": 2, "a": 1},
    "coverage_policy": {"c0": True, "c1": True, "mcdc": False},
}
CHECK = {"verdict": "FAIL", "issues": [{"code": "ORACLE_MISMATCH", "severity": "error"}]}


def _user(target, csv_text, history):
    return repair._user_prompt(target, csv_text, CHECK, {"status": "FAILED"}, history)


def test_system_prompt_is_shared_by_every_function_of_a_batch():
    reordered = dict(CONSTRAINTS, policies={"a": 1, "b": 2})

    assert repair._system_prompt(CONSTRAINTS) == repair._system_prompt(reordered)


def test_round_history_follows_the_csv_so_unchanged_rounds_share_the_prefix():
    target = {"function": "f", "source_rel": "src/f.c"}
    first = _user(target, "#COMMENT,x\n1,2", [{"seq": 1, "origin": "GENERATOR",
                                              "verdict": "FAIL", "codes": ["ORACLE_MISMATCH"]}])
    second = _user(target, "#COMMENT,x\n1,2", [
        {"seq": 1, "origin": "GENERATOR", "verdict": "FAIL", "codes": ["ORACLE_MISMATCH"]},
        {"seq": 2, "origin": "LLM", "verdict": "INVALID", "codes": []},
    ])

    shared = first[: first.index("## 已有版本的判定历史")]
    assert second.startswith(shared)
    assert shared.index("## 当前 CSV") < shared.index("## 当前版本的校验判定")


GAP = {
    "obligation_id": "b6:boundary:1:above:2", "kind": "boundary", "branch_id": "b6",
    "decision": "( ENG_PSD_OTSW_BIPOL ==\n   cu1_psd_otsw_pol )", "outcome": None,
    "condition_index": 1, "boundary_class": "above", "boundary_value": 2,
    "boundary_right_value": None, "case_label": None, "solve_status": "UNKNOWN",
    "reason": "incomplete volatile-read search",
}
COMPLETENESS = {"obligations": 163, "csv_rows": 46, "merged_by_baseline": 120,
                "unresolved_count": 1, "unresolved": [GAP]}


def test_completeness_gaps_precede_the_csv_and_are_not_repeated_in_the_verdict():
    check = {"verdict": "FAIL", "completeness": COMPLETENESS, "issues": [
        {"code": "COVERAGE_C1_INCOMPLETE", "severity": "error"},
        {"code": "INPUT_OBLIGATION_UNRESOLVED", "severity": "warning", "detail": [GAP]},
    ]}
    prompt = repair._user_prompt({"function": "f"}, "#COMMENT,x", check, None, [])

    gaps = prompt.index("## 输入完备性缺口")
    assert gaps < prompt.index("## 当前 CSV")
    assert ("- b6:boundary:1:above:2（boundary, 条件 1, above 2） 判定式："
            "( ENG_PSD_OTSW_BIPOL == cu1_psd_otsw_pol ) / 求解 UNKNOWN：incomplete volatile-read search"
            ) in prompt
    verdict = prompt[prompt.index("## 当前版本的校验判定"):]
    assert "COVERAGE_C1_INCOMPLETE" in verdict and "INPUT_OBLIGATION_UNRESOLVED" not in verdict


def test_system_prompt_keeps_expected_values_with_the_capture():
    system = repair._system_prompt(CONSTRAINTS)
    assert "期望值单元格全部留空" in system and "obligation" in system
    assert repair.PROMPT_VERSION == "ut-repair/3.1"


def test_diverged_paths_do_not_assign_an_unproven_root_cause():
    system = repair._system_prompt(CONSTRAINTS)
    assert "不能仅凭冲突认定缺少输入" in system
    assert "环境或构建原因无法排除时返回 csv:null" in system
