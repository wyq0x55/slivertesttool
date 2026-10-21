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


def test_output_oracle_noise_is_summarized_and_the_input_cause_kept():
    oracles = [f"缺少期望值 oracle: call:g:param:0:{n}" for n in range(27)]
    incomplete = "obligation evaluation UNKNOWN: semantic evaluation incomplete: call:g:param:0:0"
    noisy = dict(GAP, reason="; ".join(oracles + [incomplete]))
    line = repair._gap_line(noisy)
    assert line.endswith("：另缺 27 个输出期望值（由 WinAMS 捕获回填，不是放弃补行的理由）")
    assert "call:g:param" not in line
    mixed = dict(GAP, reason="; ".join(["intent failed validation"] + oracles))
    assert repair._gap_line(mixed).endswith("：intent failed validation；另缺 27 个输出期望值"
                                             "（由 WinAMS 捕获回填，不是放弃补行的理由）")


def test_system_prompt_asks_for_rows_not_a_csv():
    system = repair._system_prompt(CONSTRAINTS)
    assert '"rows"' in system and '"declined"' in system and "不要输出 CSV" in system
    assert "不能仅凭冲突认定缺少输入" in system
    assert repair.PROMPT_VERSION == "ut-repair/7.1"


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
    assert revision.meta["prompt_version"] == "ut-repair/7.1"


def test_a_declining_reply_adds_nothing(monkeypatch):
    revision = _revise(monkeypatch, {"rows": [], "declined": [
        {"obligation": "b6:boundary:1:above:2", "reason": "no column drives it"},
        {"obligation": "b9:T", "reason": "not offered"}]})
    assert revision.csv_bytes is None
    assert revision.meta["declined"] == ["b6:boundary:1:above:2"]
    assert "声明无法补齐 1 个" in revision.meta["error"]


def test_typed_context_and_checker_feedback_survive_as_whole_records():
    context = [{"obligation_id": GAP["obligation_id"], "path": [{
        "branch_id": "b0", "required_outcome": False, "atoms": [{
            "variable": "x", "input_tokens": ["I1"], "operator": ">=", "boundary": 5,
            "binding": "DIRECT_INPUT",
        }]}]}]
    feedback = [{"obligation_id": GAP["obligation_id"], "row": 2, "status": "REJECTED",
                 "observed": 4, "reason": "expected boundary 5", "new": True}]
    check = {**CHECK, "candidate_witnesses": {"repair_context": context, "results": feedback}}
    prompt = _user(CSV, check, [], [GAP])
    assert prompt.index("## 义务语义证据") < prompt.index("## 待补缺口")
    assert '"required_outcome": false' in prompt and '"input_tokens": ["I1"]' in prompt
    assert '"observed": 4' in prompt and "expected boundary 5" in prompt
    assert json.loads(repair._bounded_records([{"id": 1}, {"too_long": "x" * 100}], 20)) == [{"id": 1}]


def test_prefix_survives_changed_round_state_and_records_separate_hashes(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_config, "get_ai_config", lambda: {ai_config.KEY_MODEL: "m"})
    def invoke(messages, **kwargs):
        calls.append((messages, kwargs))
        kwargs["usage"].update(input_tokens=1200, output_tokens=30, cached_input_tokens=1000)
        return '{"rows": [], "declined": []}'
    monkeypatch.setattr(repair.provider, "invoke_chat", invoke)
    target = {"function": "f", "source_rel": "src/f.c"}
    prefix = repair.prompt_prefix(CONSTRAINTS, target, CSV.encode("cp932"), CHECK)
    results = []
    for history, gaps in (([], [GAP]), ([{"seq": 2, "origin": "LLM", "verdict": "FAIL", "codes": []}], [])):
        results.append(repair.revise(constraints=CONSTRAINTS, target=target, csv_bytes=CSV.encode("cp932"),
                                     check=CHECK, execution=None, history=history, open_gaps=gaps,
                                     allowed=set(), prefix_snapshot=json.loads(json.dumps(prefix))))
    assert results[0].meta["prefix"] == results[1].meta["prefix"]
    assert calls[0][0][0] == calls[1][0][0]
    assert all(messages[1]["content"].startswith(prefix["function"]) for messages, _ in calls)
    assert all(kwargs["stream"] for _, kwargs in calls)
    assert results[1].meta["usage"]["cached_input_tokens"] == 1000


def test_changed_prefix_evidence_stops_before_contacting_model(monkeypatch):
    prefix = repair.prompt_prefix(CONSTRAINTS, {"function": "old"}, CSV.encode("cp932"), CHECK)
    monkeypatch.setattr(repair.provider, "invoke_chat", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    result = repair.revise(constraints=CONSTRAINTS, target={"function": "new"}, csv_bytes=CSV.encode("cp932"),
                           check=CHECK, execution=None, history=[], open_gaps=[], allowed=set(), prefix_snapshot=prefix)
    assert result.csv_bytes is None and result.meta["code"] == "PROMPT_PREFIX_CHANGED"


def test_oversized_essential_context_is_not_sent_or_silently_truncated(monkeypatch):
    monkeypatch.setattr(repair.provider, "invoke_chat", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    constraints = dict(CONSTRAINTS, revision_rules=["x" * 130000])
    result = repair.revise(constraints=constraints, target={"function": "f"}, csv_bytes=CSV.encode("cp932"),
                           check=CHECK, execution=None, history=[], open_gaps=[], allowed=set())
    assert result.meta["code"] == "PROMPT_BUDGET_EXCEEDED"


def test_reference_rows_budget_preserves_whole_rows_and_original_numbers(monkeypatch):
    monkeypatch.setattr(repair, "_MAX_ROW_CHARS", 120)
    table = increment.parse(CSV + "".join(f",{n},0,1\r\n" for n in range(100)))
    text = repair._reference_rows(table)
    assert "#1:" in text and "省略" in text
    assert all("I1=" in line or "全部默认" in line for line in text.splitlines() if line.startswith("#"))


def test_column_compression_preserves_exact_names_and_tokens():
    names = ["long_source_path/to/file.c/global_struct.member" + str(i) for i in range(8)]
    table = increment.Table([], names, [], [])
    text = repair._columns_section(table, {})
    aliases = {}
    for line in text.splitlines():
        if line.startswith("$P"):
            key, value = line.split(" = ", 1)
            aliases[key] = json.loads(value)
        elif line.startswith("I"):
            token, name = line.split(" = ", 1)
            for key in sorted(aliases, key=len, reverse=True):
                if name.startswith(key):
                    name = aliases[key] + name[len(key):]
                    break
            assert name == names[int(token[1:]) - 1]
    assert aliases and len(text) < len(increment.columns_section(table))


def test_verified_winams_records_reach_dynamic_prompt_without_changing_prefix():
    check = dict(CHECK, coverage_feedback={"records": [
        {"report_line": 18, "c1_observation": "T/-", "mapping_status": "UNKNOWN"}]})
    prompt = _user(CSV, check, [])
    assert '"c1_observation": "T/-"' in prompt and '"mapping_status": "UNKNOWN"' in prompt
    table = increment.parse(CSV)
    assert repair._function_prompt({"function": "f"}, table, check) == repair._function_prompt({"function": "f"}, table, CHECK)


def test_evidence_beyond_render_budget_still_invalidates_prefix(monkeypatch):
    context = [{"obligation_id": f"b{i:03d}:T", "path": [{"boundary": 1, "data": "x" * 500}]}
               for i in range(100)]
    check = dict(CHECK, candidate_witnesses={"repair_context": context})
    target = {"function": "f"}
    prefix = repair.prompt_prefix(CONSTRAINTS, target, CSV.encode("cp932"), check)
    context[-1]["path"][0]["boundary"] = 99
    current = repair.prompt_prefix(CONSTRAINTS, target, CSV.encode("cp932"), check)
    assert current["function"] == prefix["function"]
    assert current["evidence_sha256"] != prefix["evidence_sha256"]
    monkeypatch.setattr(repair.provider, "invoke_chat", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    result = repair.revise(constraints=CONSTRAINTS, target=target, csv_bytes=CSV.encode("cp932"),
                           check=check, execution=None, history=[], open_gaps=[], allowed=set(), prefix_snapshot=prefix)
    assert result.meta["code"] == "PROMPT_PREFIX_CHANGED"


def _scoped_check():
    return {**CHECK, "candidate_witnesses": {
        "contract": "ut-agent.candidate-witness/1", "evidence": {"identified": True},
        "repair_context_contract": "ut-agent.repair-context/2",
        "input_scope_complete": True,
        "repair_context": [{"obligation_id": "b0:T", "path": [{"atoms": [
            {"input_tokens": ["I2"], "right_input_tokens": ["I5"], "mask_input_tokens": ["I7"]}]}]}]}}


def test_only_identified_operand_columns_are_rendered_and_rows_keep_original_tokens():
    table = increment.Table([], [f"source.c/value_{i}" for i in range(10)], [],
                            [[""] + [str(i) for i in range(10)]])
    check = _scoped_check()
    assert repair._editable_columns(table, check) == {1, 4, 6}
    assert repair._columns_section(table, check).splitlines() == [
        "I2 = source.c/value_1", "I5 = source.c/value_4", "I7 = source.c/value_6"]
    assert "#1: I2=1 I5=4 I7=6" in repair._reference_rows(table, check)
    assert "I1=" not in repair._reference_rows(table, check)
    check["candidate_witnesses"]["evidence"]["identified"] = False
    assert repair._editable_columns(table, check) is None


def test_scoped_prompt_reduces_large_table_without_renumbering_columns():
    table = increment.Table([], [f"source.c/long_global_struct.member_{i}" for i in range(718)], [], [])
    rendered = repair._function_prompt({"function": "f"}, table, _scoped_check())
    assert len(rendered) < 2000
    assert "I2 =" in rendered and "I5 =" in rendered and "I7 =" in rendered and "I1 =" not in rendered


def test_legacy_context_without_rhs_binding_contract_is_not_narrowed():
    table = increment.parse(CSV)
    check = _scoped_check()
    del check["candidate_witnesses"]["repair_context_contract"]
    assert repair._editable_columns(table, check) is None


def test_partial_direct_binding_does_not_hide_upstream_inputs():
    table = increment.parse(CSV)
    check = _scoped_check()
    check["candidate_witnesses"]["input_scope_complete"] = False
    assert repair._editable_columns(table, check) is None
    assert "I1 = a.c/x" in repair._columns_section(table, check)


def test_unmapped_runtime_coverage_gap_keeps_full_input_scope():
    table = increment.parse(CSV)
    check = _scoped_check()
    check["issues"] = [{"code": "COVERAGE_C1_INCOMPLETE", "severity": "error"}]
    assert repair._editable_columns(table, check) is None


def test_a_coverage_error_clearing_between_rounds_is_not_prefix_drift(monkeypatch):
    # Round 1 has a coverage gap, so every input column is editable; once a
    # row closes it the scope narrows to the typed operands. Same evidence.
    csv = ('mod,"a.c/f","f",8,1\r\n#COMMENT,' + ",".join(f'"a.c/v{i}"' for i in range(8)) +
           ',"a.c/f@@"\r\n,' + ",".join("0" * 8) + ",1\r\n").encode("cp932")
    target = {"function": "f", "source_rel": "src/f.c"}
    first = dict(_scoped_check(), issues=[{"code": "COVERAGE_C1_INCOMPLETE", "severity": "error"}])
    second = dict(_scoped_check(), verdict="PASS", issues=[])
    snapshot = repair.prompt_prefix(CONSTRAINTS, target, csv, first)
    assert repair.prompt_prefix(CONSTRAINTS, target, csv, second)["function"] != snapshot["function"]
    sent = []
    monkeypatch.setattr(ai_config, "get_ai_config", lambda: {ai_config.KEY_MODEL: "m"})
    monkeypatch.setattr(repair.provider, "invoke_chat",
                        lambda messages, **k: sent.append(messages) or '{"rows": [], "declined": []}')
    result = repair.revise(constraints=CONSTRAINTS, target=target, csv_bytes=csv, check=second,
                           execution=None, history=[], open_gaps=[], allowed=set(),
                           prefix_snapshot=snapshot)
    assert result.meta.get("code") is None and len(sent) == 1
    # The prompt shows the scope the validator enforces this round.
    assert "I1 =" not in sent[0][1]["content"] and "I2 =" in sent[0][1]["content"]

