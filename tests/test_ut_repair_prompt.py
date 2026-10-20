"""Repair prompt layout and the incremental-row reply contract."""

from __future__ import annotations

import json

from app.services.ai import config as ai_config
from app.services.ut_pipeline import increment, repair

CONSTRAINTS = {
    "baseline": {"id": "base", "version": "1", "source": {"document": "doc", "sheet": "s"}},
    "rules": [{"rule_id": "R1", "meaning": "rows follow the baseline", "source": "doc"}],
    "revision_rules": ["do not rename columns"],
    "policies": {"b": 2, "a": 1},
    "coverage_policy": {"c0": True, "c1": True, "mcdc": False},
}
CSV = 'mod,"a.c/f","f",2,1\r\n#COMMENT,"a.c/x","a.c/y","a.c/f@@"\r\n,0,0,1\r\n,1,0,2\r\n'
CHECK = {"verdict": "FAIL", "issues": [{"code": "ORACLE_MISMATCH", "severity": "error"}]}
GAP = {
    "obligation_id": "b6:boundary:1:above:2", "kind": "boundary", "branch_id": "b6",
    "decision": "( ENG_PSD_OTSW_BIPOL ==\n   cu1_psd_otsw_pol )", "outcome": None,
    "condition_index": 1, "boundary_class": "above", "boundary_value": 2,
    "boundary_right_value": None, "case_label": None, "solve_status": "SAT",
    "reason": "intent failed validation", "gap_class": "MISSING_CASE",
}
COMPLETENESS = {"obligations": 163, "csv_rows": 46, "merged_by_baseline": 120,
                "unresolved_count": 2, "unresolved": [GAP, dict(GAP, gap_class="SEMANTIC_EVIDENCE")]}


def _user(csv_text, check, history, open_gaps=()):
    return repair._user_prompt({"function": "f", "source_rel": "src/f.c"},
                               increment.parse(csv_text), check, {"status": "FAILED"},
                               history, list(open_gaps))


def test_system_prompt_is_shared_by_every_function_of_a_batch():
    reordered = dict(CONSTRAINTS, policies={"a": 1, "b": 2})
    assert repair._system_prompt(CONSTRAINTS) == repair._system_prompt(reordered)


def test_columns_precede_gaps_rows_verdict_and_history():
    history = [{"seq": 1, "origin": "GENERATOR", "verdict": "FAIL", "codes": ["X"]}]
    first = _user(CSV, CHECK, history)
    second = _user(CSV, CHECK, history + [{"seq": 2, "origin": "LLM", "verdict": "INVALID",
                                           "codes": [], "added": 0, "declined": 1,
                                           "rejected": ["base_row 9 超出 1..2"]}])
    shared = first[: first.index("## 已有版本的判定历史")]
    assert second.startswith(shared)
    assert "被拒：base_row 9 超出 1..2" in second
    order = [shared.index(head) for head in
             ("## 输入列", "## 待补缺口", "## 现有数据行", "## 当前版本的校验判定")]
    assert order == sorted(order)
    # The prompt carries input tokens and sparse rows, never the CSV itself.
    assert "I1 = a.c/x" in first and "#2: I1=1" in first and "```csv" not in first


def test_only_open_missing_cases_are_listed_as_work():
    check = {"verdict": "PASS", "completeness": COMPLETENESS, "issues": [
        {"code": "INPUT_OBLIGATION_UNRESOLVED", "severity": "warning", "detail": [GAP]}]}
    prompt = _user(CSV, check, [], [GAP])
    assert ("- b6:boundary:1:above:2（boundary, 条件 1, above 2） 判定式："
            "( ENG_PSD_OTSW_BIPOL == cu1_psd_otsw_pol ) / 求解 SAT：intent failed validation"
            ) in prompt
    assert "其中 1 个已补行待确认" in prompt
    verdict = prompt[prompt.index("## 当前版本的校验判定"):]
    assert "INPUT_OBLIGATION_UNRESOLVED" not in verdict


def test_system_prompt_asks_for_rows_not_a_csv():
    system = repair._system_prompt(CONSTRAINTS)
    assert '"rows"' in system and '"declined"' in system and "不要输出 CSV" in system
    assert "不能仅凭冲突认定缺少输入" in system
    assert repair.PROMPT_VERSION == "ut-repair/4.0"


def _revise(monkeypatch, reply, allowed=frozenset({"b6:boundary:1:above:2"})):
    monkeypatch.setattr(ai_config, "get_ai_config", lambda: {ai_config.KEY_MODEL: "m"})
    monkeypatch.setattr(repair.provider, "invoke_chat", lambda *a, **k: json.dumps(reply))
    return repair.revise(constraints=CONSTRAINTS, target={"function": "f"},
                         csv_bytes=CSV.encode("cp932"), check=CHECK, execution=None,
                         history=[], open_gaps=[GAP], allowed=set(allowed))


def test_reply_rows_are_appended_and_claims_recorded(monkeypatch):
    revision = _revise(monkeypatch, {"rows": [
        {"obligation": "b6:boundary:1:above:2", "base_row": 1, "inputs": {"I2": "2"},
         "reason": "y is the compared operand"},
        {"obligation": "b6:boundary:1:above:2", "base_row": 3, "inputs": {"I2": "2"}}],
        "rationale": "r"})
    assert revision.csv_bytes.decode("cp932") == CSV + ";$L$,LLM add b6:boundary:1:above:2\r\n,0,2,\r\n"
    assert revision.meta["claimed"] == ["b6:boundary:1:above:2"]
    assert [item["index"] for item in revision.meta["rejected"]] == [1]
    assert revision.meta["prompt_version"] == "ut-repair/4.0"


def test_a_declining_reply_adds_nothing(monkeypatch):
    revision = _revise(monkeypatch, {"rows": [], "declined": [
        {"obligation": "b6:boundary:1:above:2", "reason": "no column drives it"},
        {"obligation": "b9:T", "reason": "not offered"}]})
    assert revision.csv_bytes is None
    assert revision.meta["declined"] == ["b6:boundary:1:above:2"]
    assert "声明无法补齐 1 个" in revision.meta["error"]
