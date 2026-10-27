"""The repair prompt quotes the function body located by extractor provenance."""

from __future__ import annotations

import json

from app.services.ai import config as ai_config
from app.services.ut_pipeline import repair, repair_source

BODY = "int f( void )\r\n{\r\n    return table[idx];\r\n}"
CSV = 'mod,"a.c/f","f",1,1\r\n#COMMENT,"a.c/x","a.c/f@@"\r\n,0,1\r\n'


def _ir(tmp_path, *, end_delta=0, name="f", body=BODY, prefix="/* head */\r\n"):
    source = tmp_path / "a.c"
    source.write_bytes((prefix + body + "\r\n").encode("cp932"))
    start = len(prefix.encode("cp932"))
    end = start + len(body.encode("cp932")) - 1 + end_delta
    span = {"file": str(source), "line": 2, "column": 1, "offset": start, "end_offset": end}
    document = {"functions": [{"name": name, "provenance": {"spelling": span, "expansion": dict(span),
                                                            "macro_stack": []},
                               "global_objects": [{"name": "table", "is_const": True, "is_volatile": False,
                                                   "array_sizes": [1], "read": True, "write": False,
                                                   "source_file": str(source)}]}]}
    ir = tmp_path / "function-ir.json"
    ir.write_text(json.dumps(document), encoding="utf-8")
    return ir


def test_function_body_is_sliced_by_extractor_offsets(tmp_path):
    source = repair_source.load(_ir(tmp_path), "f")
    assert source["text"] == BODY
    assert source["globals"] == [{"name": "table", "is_const": True, "is_volatile": False,
                                  "array_sizes": [1], "read": True, "write": False}]
    rendered = repair_source.section(source)
    assert "2| int f( void )" in rendered and "5| }" in rendered
    assert '"array_sizes": [1]' in rendered


def test_unproven_spans_are_left_out(tmp_path):
    assert repair_source.load(_ir(tmp_path, end_delta=-1), "f") is None
    assert repair_source.load(_ir(tmp_path), "g") is None
    assert repair_source.load(tmp_path / "missing.json", "f") is None
    assert repair_source.load(None, "f") is None
    assert "未提供" in repair_source.section(None)


def test_oversize_body_is_omitted_whole(tmp_path):
    body = "int f( void )\n{\n" + "    x = 1;\n" * 3000 + "}"
    source = repair_source.load(_ir(tmp_path, body=body), "f")
    rendered = repair_source.section(source)
    assert "超过" in rendered and "x = 1;" not in rendered


def test_source_reaches_the_model_and_its_hash_binds_the_prefix(tmp_path, monkeypatch):
    source = repair_source.load(_ir(tmp_path), "f")
    calls = []
    monkeypatch.setattr(ai_config, "get_ai_config", lambda: {ai_config.KEY_MODEL: "m"})
    monkeypatch.setattr(repair.provider, "invoke_chat",
                        lambda messages, **kwargs: calls.append(messages) or '{"rows":[]}')
    check = {"verdict": "FAIL", "issues": []}
    plain = repair.prompt_prefix({}, {"function": "f"}, CSV.encode("cp932"), check)
    quoted = repair.prompt_prefix({}, {"function": "f"}, CSV.encode("cp932"), check, source)
    assert plain["evidence_sha256"] != quoted["evidence_sha256"]
    # A snapshot frozen without source no longer matches once source is known.
    stale = repair.revise(constraints={}, target={"function": "f"}, csv_bytes=CSV.encode("cp932"),
                          check=check, execution=None, history=[], open_gaps=[], allowed=set(),
                          prefix_snapshot=plain, source=source)
    assert stale.meta["code"] == "PROMPT_PREFIX_CHANGED" and not calls
    repair.revise(constraints={}, target={"function": "f"}, csv_bytes=CSV.encode("cp932"),
                  check=check, execution=None, history=[], open_gaps=[], allowed=set(),
                  prefix_snapshot=quoted, source=source)
    assert "return table[idx];" in calls[0][1]["content"]
