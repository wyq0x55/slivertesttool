"""Machine validators — the "generate → validate → retry" safety net.

These functions receive the parsed model output and return a list of
human-readable problems ([] = accepted). They are deliberately strict about
*structure* and *name existence*; semantic quality stays with the human
reviewer. Name checks are what stop the model from inventing variables that
were never in the source or SBS.
"""

from __future__ import annotations

import re
from typing import Any

_STEP_KEYS = {"no", "purpose", "operation", "subroutine", "args",
              "inputs", "expecteds", "timing"}
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\[\]]*$")


def _signal_pairs(value: Any, field: str, problems: list[str]) -> list[tuple[str, str]]:
    if not isinstance(value, list):
        problems.append(f"{field} 必须是数组")
        return []
    pairs: list[tuple[str, str]] = []
    for i, item in enumerate(value):
        if (isinstance(item, list) and len(item) == 2
                and all(isinstance(x, str) and x.strip() for x in item)):
            pairs.append((item[0].strip(), item[1].strip()))
        else:
            problems.append(
                f"{field}[{i}] 必须是 [\"表示名\",\"变量路径\"] 二元组")
    return pairs


def validate_steps_doc(doc: Any, *, known_paths: set[str] | None = None,
                       known_subs: set[str] | None = None,
                       allow_missing: bool = False) -> list[str]:
    """Validate the stored steps JSON structure (the editor's schema).

    ``known_paths``: every signal path must be either known or, when
    ``allow_missing`` is set, reported back by the caller through the
    scenario's ``missing_variables`` (procedure scenario only).
    """
    problems: list[str] = []
    if not isinstance(doc, dict):
        return ["steps_doc 必须是对象"]
    in_pairs = _signal_pairs(doc.get("input_signals"), "input_signals", problems)
    exp_pairs = _signal_pairs(doc.get("expected_signals"), "expected_signals", problems)
    paths = {p for _n, p in in_pairs + exp_pairs}
    if not paths:
        problems.append("input_signals/expected_signals 至少要有一个信号")
    if known_paths is not None:
        for path in sorted(paths):
            if path not in known_paths:
                if allow_missing:
                    continue  # scenario re-checks against missing_variables
                problems.append(f"信号路径不存在：{path}")

    steps = doc.get("steps")
    if not isinstance(steps, list) or not steps:
        problems.append("steps 必须是非空数组")
        return problems
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            problems.append(f"steps[{i}] 必须是对象")
            continue
        unknown = set(step) - _STEP_KEYS
        if unknown:
            problems.append(f"steps[{i}] 含未知字段：{sorted(unknown)}")
        if not isinstance(step.get("no"), int):
            problems.append(f"steps[{i}].no 必须是整数")
        for key in ("inputs", "expecteds"):
            cells = step.get(key)
            if cells is not None and not isinstance(cells, list):
                problems.append(f"steps[{i}].{key} 必须是数组")
        sub = step.get("subroutine")
        if sub:
            if not isinstance(sub, str) or known_subs is not None and sub not in known_subs:
                problems.append(f"steps[{i}].subroutine 引用了不存在的子程序：{sub}")
    return problems


def validate_viewpoints(parsed: Any) -> list[str]:
    if not isinstance(parsed, dict):
        return ["输出必须是对象"]
    problems: list[str] = []
    module_id = parsed.get("module_id")
    if not isinstance(module_id, str) or not module_id.strip():
        problems.append("module_id 不能为空")
    vps = parsed.get("viewpoints")
    if not isinstance(vps, list) or not vps:
        return problems + ["viewpoints 必须是非空数组"]
    seen_ids: set[str] = set()
    kinds = {"normal", "abnormal", "boundary", "combination"}
    for i, vp in enumerate(vps):
        if not isinstance(vp, dict):
            problems.append(f"viewpoints[{i}] 必须是对象")
            continue
        case_id = vp.get("case_id")
        if not isinstance(case_id, str) or not case_id.strip():
            problems.append(f"viewpoints[{i}].case_id 不能为空")
        elif case_id in seen_ids:
            problems.append(f"case_id 重复：{case_id}")
        else:
            seen_ids.add(case_id)
        if not str(vp.get("title") or "").strip():
            problems.append(f"viewpoints[{i}].title 不能为空")
        if vp.get("kind") not in kinds:
            problems.append(
                f"viewpoints[{i}].kind 必须是 {sorted(kinds)} 之一")
        if not str(vp.get("expected") or "").strip():
            problems.append(f"viewpoints[{i}].expected（期待行为）不能为空")
    return problems


def validate_sbs(parsed: Any, *, known_variables: set[str] | None) -> list[str]:
    if not isinstance(parsed, dict):
        return ["输出必须是对象"]
    problems: list[str] = []
    additions = parsed.get("sbs_additions")
    if not isinstance(additions, str) or not additions.strip():
        problems.append("sbs_additions 不能为空")
    else:
        if additions.count("{") != additions.count("}"):
            problems.append("sbs_additions 花括号不配对")
        if additions.count('"') % 2 != 0:
            problems.append("sbs_additions 引号不配对")
    needed = parsed.get("needed_variables")
    if needed is not None:
        if not isinstance(needed, list):
            problems.append("needed_variables 必须是数组")
        else:
            for i, item in enumerate(needed):
                if not isinstance(item, dict) or not str(item.get("name") or "").strip():
                    problems.append(f"needed_variables[{i}].name 不能为空")
                elif known_variables is not None and item["name"] not in known_variables:
                    problems.append(
                        f"needed_variables 中的 {item['name']} 不在源码索引中（禁止编造）")
    return problems


def validate_lib(parsed: Any, *, existing_lib_names: set[str],
                 item_ids: set[int]) -> list[str]:
    if not isinstance(parsed, dict):
        return ["输出必须是对象"]
    problems: list[str] = []
    name = parsed.get("lib_name")
    if not isinstance(name, str) or not name.strip():
        problems.append("lib_name 不能为空")
    elif name in existing_lib_names:
        problems.append(f"lib_name 与既有子程序重名：{name}")
    body = parsed.get("lib_stb")
    problems.extend(validate_steps_doc(body))
    para = parsed.get("lib_para")
    para_names: set[str] = set()
    if para is not None:
        if not isinstance(para, list):
            problems.append("lib_para 必须是数组")
        else:
            for i, item in enumerate(para):
                if not isinstance(item, dict) or not str(item.get("name") or "").strip():
                    problems.append(f"lib_para[{i}].name 不能为空")
                else:
                    para_names.add(item["name"])
    rewritten = parsed.get("rewritten")
    if rewritten is not None:
        if not isinstance(rewritten, list):
            problems.append("rewritten 必须是数组")
        else:
            subs = existing_lib_names | {name}
            for i, item in enumerate(rewritten):
                if not isinstance(item, dict):
                    problems.append(f"rewritten[{i}] 必须是对象")
                    continue
                if item.get("item_id") not in item_ids:
                    problems.append(
                        f"rewritten[{i}].item_id 不在被标注的手顺中：{item.get('item_id')}")
                problems.extend(validate_steps_doc(
                    item.get("steps_doc"), known_subs=subs))
    return problems


def validate_failure(parsed: Any) -> list[str]:
    if not isinstance(parsed, dict):
        return ["输出必须是对象"]
    problems: list[str] = []
    for key in ("analysis", "likely_cause", "suggested_action"):
        if not str(parsed.get(key) or "").strip():
            problems.append(f"{key} 不能为空")
    if parsed.get("classification") not in (
            "code_bug", "spec_gap", "test_error", "environment"):
        problems.append("classification 必须是 code_bug|spec_gap|test_error|environment 之一")
    return problems
