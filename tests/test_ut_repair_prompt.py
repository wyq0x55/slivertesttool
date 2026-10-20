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
