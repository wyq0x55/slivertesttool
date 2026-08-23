"""Prompt builders for the five AI scenarios.

All scenarios share one system prompt (role + hard rules) and require strict
JSON output; each builder assembles only the context that scenario needs —
prompts are kept small and fixed on purpose (token cost, consistency).
"""

from __future__ import annotations

from typing import Any

SYSTEM_PROMPT = (
    "你是嵌入式 ECU 软件的 SILS 测试专家，工作在 Synopsys Silver 仿真平台上。"
    "你的任务只生成测试资产草稿，最终由人工审核。\n"
    "硬性规则：\n"
    "1. 只输出 JSON，不要输出任何解释文字或 Markdown 代码块。\n"
    "2. 只能使用「已知变量/信号清单」和源码中真实存在的名字，禁止编造。\n"
    "3. 涉及既有 lib 子程序时必须复用，禁止手写等价逻辑。\n"
    "4. 不确定的内容放入对应输出的 unknown 字段说明，不要猜。"
)


def _feedback_block(feedback: list[str]) -> str:
    if not feedback:
        return ""
    items = "\n".join(f"- {p}" for p in feedback)
    return (
        "\n\n上一轮输出未通过机器校验，问题如下，请修正后重新输出完整 JSON：\n"
        + items
    )


def _section(title: str, content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, (list, dict)):
        import json
        content = json.dumps(content, ensure_ascii=False, indent=1)
    content = str(content).strip()
    if not content:
        return ""
    return f"\n\n## {title}\n{content}"


# --------------------------------------------------------------------------- #
# viewpoint：设计书 → 测试观点
# --------------------------------------------------------------------------- #
def viewpoint_messages(doc_text: str, *, module_hint: str = "",
                       feedback: list[str] | None = None) -> list[dict[str, str]]:
    user = (
        "从下面的详细设计书内容中抽取测试观点。设计书按模块规格化、每个模块有专属 ID；"
        "观点分正例/反例/边界/组合，一个模块 ID 通常对应多个观点，"
        "排列组合（多条件的真假组合）必须逐一展开成独立观点。\n"
        "输出 JSON：\n"
        '{"module_id": "设计书模块ID", "viewpoints": ['
        '{"case_id": "唯一短ID", "title": "观点标题",'
        ' "kind": "normal|abnormal|boundary|combination",'
        ' "precondition": "前置条件", "condition": "满足/违反的具体条件",'
        ' "expected": "期待行为", "variables": ["涉及变量名"]}]}'
        + _section("模块提示", module_hint)
        + _section("设计书内容", doc_text)
        + _feedback_block(feedback or [])
    )
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user}]


# --------------------------------------------------------------------------- #
# procedure：观点 + 源码上下文 → 测试手顺 steps JSON
# --------------------------------------------------------------------------- #
def procedure_messages(viewpoint: dict[str, Any], *,
                       source_context: str,
                       variable_list: list[str],
                       lib_functions: list[dict[str, Any]] | None,
                       constant_names: list[str] | None,
                       example_steps: dict[str, Any] | None,
                       feedback: list[str] | None = None) -> list[dict[str, str]]:
    user = (
        "根据测试观点和源码上下文，编写一条 Silver 测试手顺。\n"
        "手顺 JSON 结构（存储格式）：\n"
        '{"input_signals": [["表示名","变量路径"], ...],'
        ' "expected_signals": [["表示名","变量路径"], ...],'
        ' "steps": [{"no": 1, "purpose": "手順目的", "operation": "操作说明",'
        ' "inputs": ["单元格值", ...], "expecteds": ["期待值", ...],'
        ' "timing": "確認タイミング"}]}\n'
        "要求：变量路径只能取自「已知变量清单」；值单元格可写字面量或「常量名」"
        "（常量名必须取自「常量清单」）；能复用 lib 子程序的步骤用 "
        '"subroutine" 字段引用。\n'
        "输出 JSON：\n"
        '{"steps_doc": <上述结构的手顺>, "missing_variables": '
        '[{"name":"不在清单中但你认为需要的变量","type":"类型","why":"用途"}]}'
        + _section("测试观点", viewpoint)
        + _section("源码上下文", source_context)
        + _section("已知变量清单（SBS 已登记 + 源码索引）", variable_list)
        + _section("可复用 lib 子程序", lib_functions)
        + _section("常量清单", constant_names)
        + _section("范例手顺（书写风格参考）", example_steps)
        + _feedback_block(feedback or [])
    )
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user}]


# --------------------------------------------------------------------------- #
# sbs：源码 + 既有 SBS → SBS 增量 / 补变量
# --------------------------------------------------------------------------- #
def sbs_messages(source_context: str, *,
                 current_sbs: str = "",
                 needed_variables: list[str] | None = None,
                 feedback: list[str] | None = None) -> list[dict[str, str]]:
    user = (
        "根据源码生成 Silver SBS 配置草稿。若提供了既有 SBS，只输出需要"
        "追加/修改的增量内容（新模块登记、变量登记、stub 化声明），"
        "不要重复输出已有内容。\n"
        "输出 JSON：\n"
        '{"needed_variables": [{"name":"变量名","type":"类型","why":"用途"}],'
        ' "sbs_additions": "要追加到 SBS 的文本块",'
        ' "notes": "说明"}\n'
        "变量名必须与源码中的声明完全一致。"
        + _section("源码上下文", source_context)
        + _section("既有 SBS", current_sbs)
        + _section("本次必须补登记的变量", needed_variables)
        + _feedback_block(feedback or [])
    )
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user}]


# --------------------------------------------------------------------------- #
# lib：人工提议 + 被标注手顺 → 共通函数 + 手顺改写
# --------------------------------------------------------------------------- #
def lib_messages(proposal: str, *,
                 procedures: list[dict[str, Any]],
                 existing_lib_names: list[str],
                 feedback: list[str] | None = None) -> list[dict[str, str]]:
    user = (
        "测试负责人提议把多份手顺中的重复逻辑做成 lib 共通子程序。"
        "请提取共性逻辑，编写 lib 子程序，并改写引用它的手顺。\n"
        "lib 子程序格式与手顺相同（steps 结构），带形式参数表 lib_para；"
        "改写后的手顺在对应步骤用 \"subroutine\": \"子程序名\" 引用。\n"
        "输出 JSON：\n"
        '{"lib_name": "子程序名", "description": "功能说明",'
        ' "lib_para": [{"name": "参数名", "default": 0}],'
        ' "lib_stb": <steps 结构的子程序体>,'
        ' "rewritten": [{"item_id": 被改写手顺的ID, "steps_doc": <改写后手顺>}]}'
        + _section("提议说明", proposal)
        + _section("现有 lib 子程序名（不可重名）", existing_lib_names)
        + _section("被标注的手顺", procedures)
        + _feedback_block(feedback or [])
    )
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user}]


# --------------------------------------------------------------------------- #
# failure：失败日志 + 观点 → 差异分析草稿
# --------------------------------------------------------------------------- #
def failure_messages(viewpoint: dict[str, Any], *,
                     steps_doc: dict[str, Any] | None,
                     log_text: str,
                     feedback: list[str] | None = None) -> list[dict[str, str]]:
    user = (
        "一次 Silver 测试执行失败。请对比测试观点、手顺与实际日志，"
        "起草差异分析（供人工审核后作为 issue 提交）。\n"
        "输出 JSON：\n"
        '{"analysis": "差异分析", "likely_cause": "最可能原因",'
        ' "suggested_action": "建议处理",'
        ' "classification": "code_bug|spec_gap|test_error|environment"}'
        + _section("测试观点", viewpoint)
        + _section("测试手顺", steps_doc)
        + _section("失败日志（本用例段落）", log_text)
        + _feedback_block(feedback or [])
    )
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user}]
