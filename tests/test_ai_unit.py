"""AI agent package unit tests — no Flask app / no DB required.

These cover the pure layers: JSON extraction from model replies, the machine
validators, the C source indexer, and the generate → validate → retry loop
with a scripted fake provider. The HTTP/DB surface is covered separately in
``test_ai_api.py`` (needs the PostgreSQL test database).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.ai import base, c_index, provider, scenarios, validators  # noqa: E402


# --------------------------------------------------------------------------- #
# provider.extract_json
# --------------------------------------------------------------------------- #
class TestExtractJson:
    def test_plain(self):
        assert provider.extract_json('{"a": 1}') == {"a": 1}

    def test_fenced(self):
        text = '好的，结果如下：\n```json\n{"a": [1, 2]}\n```\n以上。'
        assert provider.extract_json(text) == {"a": [1, 2]}

    def test_padded_object(self):
        text = '说明文字 {"a": {"b": "}"}} 结尾说明'
        assert provider.extract_json(text) == {"a": {"b": "}"}}

    def test_braces_inside_strings(self):
        text = '前缀 {"a": "包含 } 花括号", "b": 2} 后缀'
        assert provider.extract_json(text) == {"a": "包含 } 花括号", "b": 2}

    def test_garbage_raises(self):
        with pytest.raises(ValueError):
            provider.extract_json("完全不是 JSON")


# --------------------------------------------------------------------------- #
# validators
# --------------------------------------------------------------------------- #
def _steps_doc(**over):
    doc = {
        "input_signals": [["车速", "veh_speed"]],
        "expected_signals": [["警告", "warn_flag"]],
        "steps": [{
            "no": 1, "purpose": "设置车速", "operation": "把车速设为 120",
            "inputs": ["120"], "expecteds": ["1"], "timing": "即時",
        }],
    }
    doc.update(over)
    return doc


class TestValidateStepsDoc:
    def test_ok(self):
        assert validators.validate_steps_doc(
            _steps_doc(), known_paths={"veh_speed", "warn_flag"}) == []

    def test_unknown_path_rejected(self):
        problems = validators.validate_steps_doc(
            _steps_doc(), known_paths={"veh_speed"})
        assert any("warn_flag" in p for p in problems)

    def test_unknown_path_allowed_when_declared_missing(self):
        assert validators.validate_steps_doc(
            _steps_doc(), known_paths={"veh_speed"}, allow_missing=True) == []

    def test_bad_signal_shape(self):
        doc = _steps_doc(input_signals=[["only-one"]])
        assert any("input_signals" in p for p in validators.validate_steps_doc(doc))

    def test_unknown_step_field(self):
        doc = _steps_doc()
        doc["steps"][0]["typo_field"] = 1
        assert any("未知字段" in p for p in validators.validate_steps_doc(doc))

    def test_unknown_subroutine(self):
        doc = _steps_doc()
        doc["steps"][0]["subroutine"] = "no_such_sub"
        problems = validators.validate_steps_doc(doc, known_subs=set())
        assert any("no_such_sub" in p for p in problems)


class TestValidateViewpoints:
    def _parsed(self, **over):
        doc = {
            "module_id": "MDL-001",
            "viewpoints": [
                {"case_id": "MDL001-01", "title": "正常加速", "kind": "normal",
                 "precondition": "点火ON", "condition": "车速>100",
                 "expected": "警告输出", "variables": ["veh_speed"]},
            ],
        }
        doc.update(over)
        return doc

    def test_ok(self):
        assert validators.validate_viewpoints(self._parsed()) == []

    def test_duplicate_case_id(self):
        vp = self._parsed()["viewpoints"][0]
        problems = validators.validate_viewpoints(
            self._parsed(viewpoints=[vp, dict(vp)]))
        assert any("重复" in p for p in problems)

    def test_bad_kind(self):
        problems = validators.validate_viewpoints(
            self._parsed(viewpoints=[
                dict(self._parsed()["viewpoints"][0], kind="weird")]))
        assert any("kind" in p for p in problems)

    def test_empty_viewpoints(self):
        assert validators.validate_viewpoints(
            self._parsed(viewpoints=[])) != []


class TestValidateSbsAndOthers:
    def test_sbs_ok(self):
        parsed = {"needed_variables": [{"name": "veh_speed", "type": "uint16",
                                        "why": "测试用"}],
                  "sbs_additions": "module engine { variable veh_speed; }"}
        assert validators.validate_sbs(parsed, known_variables={"veh_speed"}) == []

    def test_sbs_invented_variable(self):
        parsed = {"needed_variables": [{"name": "ghost_var"}],
                  "sbs_additions": "variable ghost_var;"}
        problems = validators.validate_sbs(parsed, known_variables={"veh_speed"})
        assert any("ghost_var" in p for p in problems)

    def test_sbs_unbalanced(self):
        parsed = {"needed_variables": [], "sbs_additions": "module {"}
        assert any("花括号" in p for p in
                   validators.validate_sbs(parsed, known_variables=set()))

    def test_lib(self):
        parsed = {
            "lib_name": "set_speed",
            "description": "设置车速",
            "lib_para": [{"name": "speed", "default": 0}],
            "lib_stb": _steps_doc(),
            "rewritten": [{"item_id": 7, "steps_doc": _steps_doc()}],
        }
        problems = validators.validate_lib(
            parsed, existing_lib_names={"other"}, item_ids={7})
        assert problems == []

    def test_lib_duplicate_name(self):
        parsed = {"lib_name": "existing", "lib_stb": _steps_doc()}
        problems = validators.validate_lib(
            parsed, existing_lib_names={"existing"}, item_ids=set())
        assert any("重名" in p for p in problems)

    def test_failure(self):
        ok_doc = {"analysis": "差异", "likely_cause": "原因",
                  "suggested_action": "对策", "classification": "code_bug"}
        assert validators.validate_failure(ok_doc) == []
        assert validators.validate_failure(dict(ok_doc, classification="x"))


# --------------------------------------------------------------------------- #
# C source indexer
# --------------------------------------------------------------------------- #
_SOURCE = """
#include "engine.h"
uint16_t veh_speed; /* 車速センサ値 */
static uint8_t engine_state = 0;
const uint32_t odometer_main;

int engine_init(void)
{
    return 0;
}

uint16_t engine_read_speed(const uint8_t channel)
{
    return veh_speed + channel;
}
"""


class TestCIndex:
    def test_globals_and_functions(self):
        index = c_index.index_source({"engine.c": _SOURCE})
        assert "veh_speed" in index["variables"]
        assert "engine_state" in index["variables"]
        assert "odometer_main" in index["variables"]
        assert "engine_init" in index["functions"]
        assert "engine_read_speed" in index["functions"]
        names = [g["name"] for g in index["files"]["engine.c"]["globals"]]
        assert "veh_speed" in names

    def test_block_locals_are_not_globals(self):
        # A local declared flush-left inside a function body is invisible to
        # the AST's file scope — the exact case the old regex backend got
        # wrong and let into prompts as a settable "global".
        src = "int real_global;\nvoid f(void)\n{\nlocal_var = 1;\nint flush_local;\n}\n"
        index = c_index.index_source({"a.c": src})
        assert "real_global" in index["variables"]
        assert "local_var" not in index["variables"]
        assert "flush_local" not in index["variables"]

    def test_multi_declarator_line(self):
        # ``int a, b;`` — both names must appear (regex backends typically
        # catch only the first).
        index = c_index.index_source({"a.c": "int a, b;\n"})
        assert {"a", "b"} <= set(index["variables"])

    def test_ifdef_resolved_by_compile_args(self):
        src = (
            "#ifdef TARGET_A\nuint8_t var_a;\n#endif\n"
            "#ifdef TARGET_B\nuint8_t var_b;\n#endif\n"
        )
        only_a = c_index.index_source({"a.c": src}, compile_args=["-DTARGET_A"])
        assert "var_a" in only_a["variables"]
        assert "var_b" not in only_a["variables"]
        both = c_index.index_source({"a.c": src},
                                    compile_args=["-DTARGET_A", "-DTARGET_B"])
        assert {"var_a", "var_b"} <= set(both["variables"])

    def test_inventory_lines(self):
        index = c_index.index_source({"engine.c": _SOURCE})
        inv = c_index.variable_inventory(index)
        assert any(line.startswith("veh_speed : ") for line in inv)

    def test_select_context_matches_keyword(self):
        index = c_index.index_source({"engine.c": _SOURCE})
        picked = c_index.select_context(index, ["veh_speed", "加速"])
        assert "engine.c" in picked

    def test_select_context_empty_when_no_match(self):
        index = c_index.index_source({"engine.c": _SOURCE})
        assert c_index.select_context(index, ["不存在的关键字"]) == {}


# --------------------------------------------------------------------------- #
# generate → validate → retry loop, with a scripted fake provider
# --------------------------------------------------------------------------- #
class FakeChat:
    """Returns queued replies; records every call for assertions."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    def __call__(self, messages, **_kwargs):
        self.calls.append(messages)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _run_loop(fake, validate):
    def build(feedback):
        content = "prompt"
        if feedback:
            content += "\n".join(feedback)
        return [{"role": "user", "content": content}]

    return base.generate_validated(
        build_prompt=build, validate=validate,
        max_rounds=3, temperature=0.0)


class TestRetryLoop:
    def test_first_round_accept(self, monkeypatch):
        fake = FakeChat(['{"ok": true}'])
        monkeypatch.setattr(provider, "chat", fake)
        result = _run_loop(fake, lambda parsed: [])
        assert result.output == {"ok": True}
        assert result.rounds == 1

    def test_retry_then_accept(self, monkeypatch):
        fake = FakeChat(['{"ok": 1}', '{"ok": 2}'])
        monkeypatch.setattr(provider, "chat", fake)
        seen: list[Any] = []

        def validate(parsed):
            seen.append(parsed["ok"])
            return [] if parsed["ok"] == 2 else ["值不对"]

        result = _run_loop(fake, validate)
        assert result.output == {"ok": 2}
        assert result.rounds == 2
        assert seen == [1, 2]
        # Round 2's prompt must carry the round-1 feedback.
        assert any("值不对" in m["content"] for m in fake.calls[1])

    def test_exhausted_raises(self, monkeypatch):
        fake = FakeChat(['{"ok": 1}'] * 3)
        monkeypatch.setattr(provider, "chat", fake)
        with pytest.raises(base.GenerationError):
            _run_loop(fake, lambda parsed: ["始终不行"])

    def test_validator_crash_is_a_problem_not_an_infinite_loop(self, monkeypatch):
        fake = FakeChat(['{"ok": 1}'] * 3)
        monkeypatch.setattr(provider, "chat", fake)

        def boom(_parsed):
            raise RuntimeError("validator bug")

        with pytest.raises(base.GenerationError):
            _run_loop(fake, boom)


# --------------------------------------------------------------------------- #
# scenario smoke tests (fake provider, real validators + indexer)
# --------------------------------------------------------------------------- #
class TestRegistry:
    def test_from_index_uses_declaration_comment(self):
        from app.services.ai import registry as reg_mod
        reg = reg_mod.build(index=c_index.index_source({"engine.c": _SOURCE}))
        assert reg.display("veh_speed") == "車速センサ値"
        assert "veh_speed（車速センサ値）: uint16_t" in reg.prompt_lines()

    def test_history_overrides_comment(self):
        from app.services.ai import registry as reg_mod
        reg = reg_mod.build(
            index=c_index.index_source({"engine.c": _SOURCE}),
            historical_pairs=[["実车速", "veh_speed"], ["実车速", "veh_speed"],
                              ["别的", "veh_speed"]])
        assert reg.display("veh_speed") == "実车速"  # 最频繁者胜

    def test_sbs_text_mining(self):
        from app.services.ai import registry as reg_mod
        sbs = 'engine.veh_speed "車速センサ値" : uint16;\nother_var : uint8; // 備考\n'
        reg = reg_mod.build(
            index=c_index.index_source({"engine.c": _SOURCE}),
            sbs_text=sbs,
            sbs_variables=[["警告出力", "warn_flag"]])
        assert "veh_speed" in reg
        assert "warn_flag" in reg
        assert reg.display("warn_flag") == "警告出力"

    def test_viewpoint_seeds_come_through_scenario(self, monkeypatch):
        # covered end-to-end below; here just the pair normalisation
        from app.services.ai.scenarios import _viewpoint_seeds
        seeds = _viewpoint_seeds([
            {"variables": [["車速", "veh_speed"], "plain_name"]},
        ])
        assert seeds == [["veh_speed", "車速"], "plain_name"]

    def test_resolve_ambiguity_raises(self):
        from app.services.ai import registry as reg_mod
        reg = reg_mod.Registry()
        reg.add("a.speed", "速度", source="t", prio=0)
        reg.add("b.speed", "速度", source="t", prio=0)
        with pytest.raises(ValueError):
            reg.resolve("speed")

    def test_resolve_by_display_and_tail(self):
        from app.services.ai import registry as reg_mod
        reg = reg_mod.Registry()
        reg.add("engine.veh_speed", "車速", source="t", prio=0)
        assert reg.resolve("engine.veh_speed") == "engine.veh_speed"
        assert reg.resolve("車速") == "engine.veh_speed"
        assert reg.resolve("veh_speed") == "engine.veh_speed"


class TestSparseExpansion:
    def _registry(self):
        from app.services.ai import registry as reg_mod
        reg = reg_mod.Registry()
        reg.add("veh_speed", "車速センサ値", source="t", prio=0, type_="uint16_t")
        reg.add("warn_flag", "警告フラグ", source="t", prio=0, type_="uint8_t")
        return reg

    def test_expand_backfills_display_and_blanks(self):
        from app.services.ai import sparse
        sparse_doc = {"steps": [
            {"no": 1, "purpose": "设定车速", "operation": "120",
             "inputs": {"veh_speed": "120"}, "timing": "即時"},
            {"no": 2, "purpose": "确认警告", "operation": "確認",
             "expecteds": {"warn_flag": "1"}, "timing": "即時"},
        ]}
        doc, problems = sparse.expand_procedure(sparse_doc, self._registry())
        assert problems == []
        assert doc["input_signals"] == [["車速センサ値", "veh_speed"]]
        assert doc["expected_signals"] == [["警告フラグ", "warn_flag"]]
        assert doc["steps"][0]["inputs"] == ["120"]
        assert doc["steps"][0].get("expecteds") is None  # nothing expected yet
        # Step 2 keeps the input column as blank (unchanged signal).
        assert doc["steps"][1].get("inputs") is None
        assert doc["steps"][1]["expecteds"] == ["1"]

    def test_unknown_name_rejected(self):
        from app.services.ai import sparse
        doc, problems = sparse.expand_procedure(
            {"steps": [{"no": 1, "inputs": {"ghost": "1"}}]},
            self._registry())
        assert doc == {}
        assert any("ghost" in p for p in problems)

    def test_allowed_missing_skipped(self):
        from app.services.ai import sparse
        doc, problems = sparse.expand_procedure(
            {"steps": [{"no": 1, "inputs": {"veh_speed": "1",
                                            "later_var": "2"}}]},
            self._registry(), allowed_missing={"later_var"})
        assert problems == []
        assert doc["input_signals"] == [["車速センサ値", "veh_speed"]]


class TestScenarios:
    def test_viewpoint(self, monkeypatch):
        reply = json.dumps({
            "module_id": "MDL-002",
            "viewpoints": [
                {"case_id": "MDL002-01", "title": "超速警告·正例",
                 "kind": "normal", "precondition": "IG ON",
                 "condition": "veh_speed > 100", "expected": "warn_flag=1",
                 "variables": ["veh_speed"]},
                {"case_id": "MDL002-02", "title": "超速警告·反例",
                 "kind": "abnormal", "precondition": "IG ON",
                 "condition": "veh_speed <= 100", "expected": "warn_flag=0",
                 "variables": ["veh_speed"]},
            ],
        }, ensure_ascii=False)
        monkeypatch.setattr(provider, "chat", FakeChat([reply]))
        result = scenarios.generate_viewpoint(
            {"doc_text": "模块 MDL-002：车速超过 100km/h 时输出超速警告。"})
        assert len(result.output["viewpoints"]) == 2

    def test_viewpoint_requires_doc(self):
        with pytest.raises(ValueError):
            scenarios.generate_viewpoint({"doc_text": "  "})

    def test_procedure_two_phase_with_targeted_retry(self, monkeypatch):
        # Call 1 (plan): valid mapping. Call 2 (batch): one item with an
        # invented signal. Call 3 (batch retry of that item only): good.
        plan_reply = json.dumps({"plans": [
            {"ref": "VP-01", "precond": {},
             "goal": {"veh_speed": "120"},
             "expected": {"warn_flag": "1"}, "notes": ""},
        ]}, ensure_ascii=False)
        bad_batch = json.dumps({"procedures": [
            {"ref": "VP-01", "steps": [
                {"no": 1, "purpose": "設定", "operation": "120",
                 "inputs": {"ghost_speed": "120"},
                 "expecteds": {"warn_flag": "1"}, "timing": "即時"}],
             "missing_variables": []},
        ]}, ensure_ascii=False)
        good_batch = json.dumps({"procedures": [
            {"ref": "VP-01", "steps": [
                {"no": 1, "purpose": "設定", "operation": "120",
                 "inputs": {"veh_speed": "120"},
                 "expecteds": {"warn_flag": "1"}, "timing": "即時"}],
             "missing_variables": []},
        ]}, ensure_ascii=False)
        fake = FakeChat([plan_reply, bad_batch, good_batch])
        monkeypatch.setattr(provider, "chat", fake)
        result = scenarios.generate_procedure({
            "viewpoints": [{"ref": "VP-01", "case_id": "MDL100-01",
                            "title": "超速警告·正例",
                            "condition": "veh_speed > 100",
                            "expected": "warn_flag = 1",
                            "variables": ["veh_speed"]}],
            "source_files": {"engine.c": _SOURCE},
            "sbs_variables": [["警告フラグ", "warn_flag"]],
        })
        assert result.output["failed_refs"] == []
        proc = result.output["procedures"][0]
        assert proc["ref"] == "VP-01"
        assert proc["steps_doc"]["input_signals"] == [["車速センサ値", "veh_speed"]]
        assert proc["steps_doc"]["steps"][0]["inputs"] == ["120"]
        # Three LLM calls: plan, failed round, targeted retry round.
        assert len(fake.calls) == 3
        # The retry prompt carries only the failed item's feedback.
        assert any("ghost_speed" in m["content"] for m in fake.calls[2])

    def test_procedure_persists_failed_refs_instead_of_raising(self, monkeypatch):
        plan_reply = json.dumps({"plans": [
            {"ref": "VP-01", "goal": {"veh_speed": "120"},
             "expected": {"warn_flag": "1"}},
        ]}, ensure_ascii=False)
        bad_batch = json.dumps({"procedures": [
            {"ref": "VP-01", "steps": [
                {"no": 1, "inputs": {"ghost_speed": "1"}}],
             "missing_variables": []},
        ]}, ensure_ascii=False)
        monkeypatch.setattr(provider, "chat",
                            FakeChat([plan_reply, bad_batch, bad_batch,
                                      bad_batch]))
        result = scenarios.generate_procedure({
            "viewpoints": [{"ref": "VP-01", "title": "t"}],
            "source_files": {"engine.c": _SOURCE},
            "sbs_variables": [["警告フラグ", "warn_flag"]],
        })
        assert result.output["procedures"] == []
        assert result.output["failed_refs"] == ["VP-01"]

    def test_procedure_requires_signals(self):
        with pytest.raises(ValueError):
            scenarios.generate_procedure({
                "viewpoints": [{"ref": "1", "title": "t"}],
                "source_files": {},
            })

    def test_procedure_cross_check_catches_untested_plan_var(self, monkeypatch):
        # The plan expects warn_flag, but the procedure never checks it —
        # "漂亮但没测到点上" must be caught by the cross-check, retried, then
        # reported in failed_refs when still missing.
        plan_reply = json.dumps({"plans": [
            {"ref": "VP-01", "goal": {"veh_speed": "120"},
             "expected": {"warn_flag": "1"}},
        ]}, ensure_ascii=False)
        no_check = json.dumps({"procedures": [
            {"ref": "VP-01", "steps": [
                {"no": 1, "purpose": "設定", "operation": "120",
                 "inputs": {"veh_speed": "120"}, "timing": "即時"}],
             "missing_variables": []},
        ]}, ensure_ascii=False)
        monkeypatch.setattr(provider, "chat",
                            FakeChat([plan_reply] + [no_check] * 3))
        result = scenarios.generate_procedure({
            "viewpoints": [{"ref": "VP-01", "title": "t"}],
            "source_files": {"engine.c": _SOURCE},
            "sbs_variables": [["警告フラグ", "warn_flag"]],
        })
        assert result.output["failed_refs"] == ["VP-01"]

    def test_sbs(self, monkeypatch):
        reply = json.dumps({
            "needed_variables": [{"name": "veh_speed", "type": "uint16",
                                  "why": "手顺需要设定"}],
            "sbs_additions": 'variable veh_speed : uint16;',
            "notes": "",
        })
        monkeypatch.setattr(provider, "chat", FakeChat([reply]))
        result = scenarios.generate_sbs({"source_files": {"engine.c": _SOURCE}})
        assert "veh_speed" in result.output["sbs_additions"]

    def test_sbs_requires_source(self):
        with pytest.raises(ValueError):
            scenarios.generate_sbs({"source_files": {}})

    def test_lib(self, monkeypatch):
        reply = json.dumps({
            "lib_name": "set_speed",
            "description": "设置车速并确认警告",
            "lib_para": [{"name": "speed", "default": 0}],
            "lib_stb": _steps_doc(),
            "rewritten": [{"item_id": 5, "steps_doc": _steps_doc()}],
        }, ensure_ascii=False)
        monkeypatch.setattr(provider, "chat", FakeChat([reply]))
        result = scenarios.generate_lib({
            "proposal": "多份手顺重复设置车速",
            "procedures": [{"item_id": 5, "case_id": "T-005",
                            "steps_doc": _steps_doc()}],
            "existing_lib_names": [],
        })
        assert result.output["lib_name"] == "set_speed"

    def test_failure(self, monkeypatch):
        reply = json.dumps({
            "analysis": "期待 warn_flag=1 实际 0",
            "likely_cause": "阈值常量加载错误",
            "suggested_action": "确认 const 表",
            "classification": "test_error",
        }, ensure_ascii=False)
        monkeypatch.setattr(provider, "chat", FakeChat([reply]))
        result = scenarios.generate_failure({
            "viewpoint": {"title": "超速警告"},
            "log_text": "Step.1 is failed (warn_flag expected 1 actual 0)",
        })
        assert result.output["classification"] == "test_error"

    def test_run_scenario_dispatch(self):
        with pytest.raises(ValueError):
            scenarios.run_scenario("nope", {})
