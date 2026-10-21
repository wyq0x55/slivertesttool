"""Model rows are appended to a TestCsv; nothing that exists can change."""

from __future__ import annotations

from app.services.ut_pipeline import increment

CSV = ('mod,"a.c/f","f",3,1,,,,CPP,,,"",0\r\n'
       '%,"AMSTB_g","g"\r\n'
       '#COMMENT,"a.c/x","a.c/y","a.c/z","a.c/f@@"\r\n'
       ';$L$,if ( x == 1 )\r\n'
       ',1,0,0,5\r\n'
       ',0,0,0,7\r\n'
       ',0,2,0,9\r\n')


def _table():
    return increment.parse(CSV)


def test_prompt_sections_use_tokens_and_list_only_non_default_cells():
    table = _table()
    assert increment.columns_section(table) == "I1 = a.c/x\nI2 = a.c/y\nI3 = a.c/z"
    rows = increment.rows_section(table).split("\n")
    assert rows[1:] == ["#1: I1=1", "#2: （全部默认）", "#3: I2=2"]


def test_accepted_rows_are_appended_with_blank_expected_values_and_a_label():
    table = _table()
    applied = increment.apply(table, [
        {"obligation": "b1:T", "base_row": 2, "inputs": {"I3": "4"}, "reason": "z drives b1"},
        {"obligation": "COVERAGE_C1_INCOMPLETE", "base_row": 1, "inputs": {"a.c/y": "-1"}},
    ], {"b1:T", "COVERAGE_C1_INCOMPLETE"})
    assert applied.rejected == []
    text = applied.csv_bytes.decode("cp932")
    # Every existing line is kept byte for byte, then the new rows follow.
    assert text.startswith(CSV)
    assert text[len(CSV):] == (";$L$,LLM add b1:T\r\n,0,0,4,\r\n"
                               ";$L$,LLM add COVERAGE_C1_INCOMPLETE\r\n,1,-1,0,\r\n")
    assert [(row["obligation"], row["row"], row["inputs"]) for row in applied.accepted] == [
        ("b1:T", 4, {"I3": "4"}), ("COVERAGE_C1_INCOMPLETE", 5, {"I2": "-1"})]
    assert len(increment.parse(text).rows) == 5


def test_invalid_rows_are_rejected_one_by_one():
    table = _table()
    applied = increment.apply(table, [
        {"obligation": "b9:T", "base_row": 1, "inputs": {"I1": "2"}},       # not offered
        {"obligation": "b1:T", "base_row": 9, "inputs": {"I1": "2"}},       # no such row
        {"obligation": "b1:T", "base_row": 1, "inputs": {"I4": "2"}},       # output column
        {"obligation": "b1:T", "base_row": 1, "inputs": {"I1": "1,2"}},     # unsafe value
        {"obligation": "b1:T", "base_row": 1, "inputs": {"I1": "0"}},       # duplicates row 2
        {"obligation": "b1:T", "base_row": 1, "inputs": {"I1": "1"}},       # unchanged
        {"obligation": "b1:T", "base_row": 1, "inputs": {"I2": "3"}},       # valid
    ], {"b1:T"})
    assert [item["index"] for item in applied.rejected] == [0, 1, 2, 3, 4, 5]
    assert [row["inputs"] for row in applied.accepted] == [{"I2": "3"}]
    assert applied.csv_bytes.decode("cp932").endswith(",1,3,0,\r\n")


def test_nothing_accepted_returns_no_csv():
    applied = increment.apply(_table(), [], {"b1:T"})
    assert applied.csv_bytes is None and applied.accepted == []
    assert increment.apply(_table(), "rows", {"b1:T"}).rejected


def test_hidden_input_columns_cannot_be_changed_but_are_copied_from_base():
    result = increment.apply(_table(), [
        {"obligation": "b1:T", "base_row": 1, "inputs": {"I1": "2"}},
        {"obligation": "b1:T", "base_row": 1, "inputs": {"a.c/x": "2"}},
        {"obligation": "b1:T", "base_row": 1, "inputs": {"I3": "4"}},
    ], {"b1:T"}, editable_columns={2})
    assert len(result.rejected) == 2 and len(result.accepted) == 1
    assert result.csv_bytes.decode("cp932").endswith(",1,0,4,\r\n")


def test_a_csv_without_rows_starts_from_a_blank_row():
    empty = increment.parse(CSV.split(";$L$")[0])
    assert increment.rows_section(empty).startswith("无数据行")
    applied = increment.apply(empty, [{"obligation": "b1:T", "base_row": 0,
                                       "inputs": {"I2": "1"}}], {"b1:T"})
    assert applied.csv_bytes.decode("cp932").endswith(";$L$,LLM add b1:T\r\n,,1,,\r\n")


def test_typed_bounds_reject_a_proposal_before_appending_and_keep_legal_rows():
    table = _table()
    domains = {"contract": "ut-agent.input-domains/1", "columns": [
        {"index": i, "column": name, "status": "PROVEN" if i == 3 else "UNKNOWN",
         "min": 0, "max": 1, "source": "value_type_facts.global.z"}
        for i, name in enumerate(table.inputs, 1)]}
    proposals = [
        {"obligation": "b1:T", "base_row": 1, "inputs": {"I3": str(value)}}
        for value in (5, 1)]
    result = increment.apply(table, proposals, {"b1:T"}, domains)
    assert result.csv_bytes.decode("cp932").endswith(",1,0,1,\r\n")
    assert result.accepted[0]["inputs"] == {"I3": "1"}
    assert "INPUT_OUT_OF_RANGE" in result.rejected[0]["error"]
    assert "允许 0..1" in result.rejected[0]["error"]
    assert "I3 = a.c/z [范围 0..1]" in increment.columns_section(table, domains)


def test_proven_numeric_equivalence_cannot_evade_duplicate_detection():
    table = _table()
    domains = {"contract": "ut-agent.input-domains/1", "columns": [
        {"index": i, "column": name, "status": "PROVEN", "min": 0, "max": 255}
        for i, name in enumerate(table.inputs, 1)]}
    result = increment.apply(table, [{"obligation": "b1:T", "base_row": 2,
                                      "inputs": {"I1": "0x1"}}], {"b1:T"}, domains)
    assert result.csv_bytes is None and "完全相同" in result.rejected[0]["error"]
