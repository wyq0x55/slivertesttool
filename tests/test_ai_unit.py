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
uint16_t veh_speed;
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

    def test_procedure_uses_source_index_for_names(self, monkeypatch):
        # The model tries to sneak in an invented signal; the validator must
        # reject round 1 and accept round 2 (real name + declared missing).
        bad = json.dumps({
            "steps_doc": _steps_doc(input_signals=[["车速", "ghost_speed"]]),
            "missing_variables": [],
        })
        good = json.dumps({
            "steps_doc": _steps_doc(),
            "missing_variables": [
                {"name": "warn_flag", "type": "uint8", "why": "SBS 未登记"}],
        }, ensure_ascii=False)
        # good uses veh_speed (in source) for input and warn_flag (NOT in
        # source, declared missing) for expected — accepted by allow_missing.
        monkeypatch.setattr(provider, "chat", FakeChat([bad, good]))
        result = scenarios.generate_procedure({
            "viewpoint": {"title": "超速警告", "variables": ["veh_speed"],
                          "condition": "veh_speed > 100", "expected": "warn=1"},
            "source_files": {"engine.c": _SOURCE},
            "sbs_variables": [],
            "lib_functions": [],
        })
        assert result.rounds == 2
        assert result.output["steps_doc"]["steps"][0]["no"] == 1

    def test_procedure_undeclared_unknown_rejected(self, monkeypatch):
        bad = json.dumps({
            "steps_doc": _steps_doc(input_signals=[["车速", "ghost_speed"]]),
            "missing_variables": [],
        })
        monkeypatch.setattr(provider, "chat", FakeChat([bad] * 3))
        with pytest.raises(base.GenerationError):
            scenarios.generate_procedure({
                "viewpoint": {"title": "t"},
                "source_files": {"engine.c": _SOURCE},
            })

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
