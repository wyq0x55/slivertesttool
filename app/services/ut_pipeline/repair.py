"""Local-LLM input rows for a TestCsv that the ut-agent check left incomplete.

The model receives the constraint pack exported by ut-agent for the bound
baseline (the same rules the checker enforces), the open missing-case gaps
of the target, the input columns and the existing rows, and the machine
verdict.  It returns input rows only: an obligation, a reference row and the
input cells to change.  The platform appends them with blank expected values
(see ``increment``), so existing rows, columns and expected values cannot
change.  Blank expected values are settled by the WinAMS capture, and every
outcome is executed, checked again and still ends at a human gate.

Prompt layout follows prefix-cache reuse on the local model: the system
message depends only on the batch constraint pack (shared by every function
of the batch), and the user message runs from the most to the least stable
part: target and input columns (fixed per job), open gaps, existing rows,
the verdict, then the round history, which changes on every round.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Optional

from ..ai import config as ai_config
from ..ai import provider
from . import increment

PROMPT_VERSION = "ut-repair/5.0"
# Bound the prompt: issue details are already truncated by ut-agent.
_MAX_DETAIL_CHARS = 6000
_MAX_DECISION_CHARS = 200
# Reported in the gap section; repeating it in the verdict costs tokens.
_GAP_CODE = "INPUT_OBLIGATION_UNRESOLVED"


@dataclass
class Revision:
    csv_bytes: Optional[bytes]
    meta: dict[str, Any] = field(default_factory=dict)


def _system_prompt(constraints: dict[str, Any]) -> str:
    baseline = constraints.get("baseline") or {}
    source = baseline.get("source") or {}
    coverage = constraints.get("coverage_policy") or {}
    rules = "\n".join(
        f"- {rule.get('rule_id')}: {rule.get('meaning')}（{rule.get('source', '')}）"
        for rule in constraints.get("rules", ())
    )
    revision_rules = "\n".join(f"- {rule}" for rule in constraints.get("revision_rules", ()))
    policies = json.dumps(constraints.get("policies", {}), ensure_ascii=False, sort_keys=True)
    return (
        "你是 WinAMS 单元测试用例（TestCsv）的输入手順助手。你只提出新增的输入行，"
        "平台按你的提议追加行并交给 ut-agent 校验器重新执行与判定，最终由人工审核。\n\n"
        "## 硬性约束（校验器逐条检查，违反即判失败）\n"
        f"{revision_rules}\n\n"
        f"## 项目绑定的测试基准 {baseline.get('id')}@{baseline.get('version')}"
        f"（{source.get('document', '')} / {source.get('sheet', '')}）\n"
        f"{rules}\n\n策略参数：{policies}\n\n"
        f"## 覆盖要求\nC0: {coverage.get('c0')} / C1: {coverage.get('c1')} / "
        f"MC/DC: {coverage.get('mcdc')}（true 表示必须 100%）\n\n"
        "## 输出\n只输出一个 JSON 对象，不要输出其他文字：\n"
        '{"rows": [{"obligation": "<缺口义务 ID 或校验问题代码>", "base_row": <参考行编号>, '
        '"inputs": {"I<n>": "<取值>"}, "reason": "<取值依据>"}], '
        '"declined": [{"obligation": "<义务 ID>", "reason": "<无法补齐的原因>"}], '
        '"rationale": "<总体说明>"}\n\n'
        "## 方式\n"
        "- 不要输出 CSV。每个新增行 = 复制 base_row 指定的现有数据行，只改 inputs 中列出的输入列；"
        "base_row 0 表示全部输入为空的行。\n"
        "- 输入列用 I1..In 引用（见「输入列」）；不能改输出列，平台把新增行的期望值全部留空，由 WinAMS 执行捕获回填。\n"
        "- 已有行、列与期望值由平台保持不变；期望值与实际值不一致由平台用执行捕获值覆盖，不需要你处理。\n"
        "- obligation 只能是「待补缺口」中的义务 ID，或「校验判定」中的问题代码（例如覆盖率不足）。\n"
        "- 依据判定式与列含义推出取值并写在 reason 中；不要猜测，推不出的义务放进 declined 并说明原因。\n"
        "- 新增行的输入不能与任何已有行完全相同。\n"
        "- 程序会用实际新增行重新检查义务、父分支路径和 MC/DC 配对；认领义务不等于满足义务。"
        "被拒绝的行不会进入 WinAMS，下一轮请依据程序反馈修正输入，而非重复相同行。\n"
        "- 一轮候选被拒绝时，当前 CSV 保持不变。该轮中已证实的提议如需保留，须与修正行一起重新提出。\n"
        "- 只能依据给出的精确输入映射或类型化依赖证据推导控制关系；NO_DIRECT_INPUT 表示没有直接列映射，"
        "不能用名称相似的缓冲区字段代替 ROM 配置或内部局部变量。证据不足时写入 declined。\n"
        "- EXECUTION_PATH_DIVERGED 表示桩函数调用次数与预测冲突；可能来自输入手順、构建或运行配置，"
        "不能仅凭冲突认定缺少输入。只在给定语义证据支持时新增行；否则不新增，在 rationale 中说明，交由人工检查。"
    )


def _gap_line(item: dict[str, Any]) -> str:
    facts = [item.get("kind") or "?"]
    if item.get("outcome") is not None:
        facts.append(f"outcome={item['outcome']}")
    if item.get("condition_index") is not None:
        facts.append(f"条件 {item['condition_index']}")
    if item.get("boundary_class"):
        bound = item.get("boundary_value")
        if item.get("boundary_right_value") is not None:
            bound = f"{bound}..{item['boundary_right_value']}"
        facts.append(f"{item['boundary_class']} {bound}")
    if item.get("case_label"):
        facts.append(f"case {item['case_label']}")
    decision = " ".join(str(item.get("decision") or item.get("description") or "").split())
    line = (f"- {item.get('obligation_id')}（{', '.join(facts)}）"
            f" 判定式：{decision[:_MAX_DECISION_CHARS]}"
            f" / 求解 {item.get('solve_status')}")
    constraints = [f"{c.get('variable')} {c.get('operator')} {c.get('value')}"
                   for c in item.get("constraints") or ()]
    if constraints:
        line += f" / 求解约束：{'; '.join(constraints)}"
    if item.get("reason"):
        line += f"：{item['reason'][:_MAX_DECISION_CHARS]}"
    return line


def _gaps_section(completeness: Optional[dict[str, Any]], open_gaps: list[dict[str, Any]]) -> str:
    if not completeness:
        return "## 待补缺口\n生成器未提供义务清单，按覆盖率与校验结果补行。"
    head = (f"Baseline 义务 {completeness.get('obligations')} 个，生成 {completeness.get('csv_rows')} 行，"
            f"按基准合并 {completeness.get('merged_by_baseline')} 个，"
            f"未解决 {completeness.get('unresolved_count')} 个。")
    others = (completeness.get("unresolved_count") or 0) - len(open_gaps)
    if others > 0:
        head += (f"其中 {others} 个已补行待确认、已声明无法补齐，或缺的是语义/环境证据、待人工确认不可达，"
                 "不要为它们新增行。")
    gaps = "\n".join(_gap_line(item) for item in open_gaps)
    return f"## 待补缺口（缺用例：生成器没有给出已证明输入的义务）\n{head}\n{gaps or '- 无'}"


def _user_prompt(target: dict[str, Any], table: increment.Table, check: dict[str, Any],
                 execution: Optional[dict[str, Any]], history: list[dict[str, Any]],
                 open_gaps: list[dict[str, Any]]) -> str:
    issues = _bounded_records(
        [issue for issue in check.get("issues", ()) if issue.get("code") not in
         {_GAP_CODE, "OUTPUT_ORACLE_PENDING"}],
        _MAX_DETAIL_CHARS)
    execution_summary = json.dumps(
        {key: (execution or {}).get(key) for key in ("status", "exit_code", "diagnostic", "coverage")},
        ensure_ascii=False,
    )
    rounds = "\n".join(_history_line(item) for item in history) or "- 无"
    witnesses = check.get("candidate_witnesses") or {}
    context = witnesses.get("repair_context") or []
    # Whole records only: never cut a typed expression halfway through JSON.
    typed = []
    for record in context:
        expanded = json.dumps(typed + [record], ensure_ascii=False, sort_keys=True)
        if len(expanded) > 16000:
            break
        typed.append(record)
    semantic_context = json.dumps(typed, ensure_ascii=False, sort_keys=True)
    feedback = json.dumps([row for row in witnesses.get("results") or () if row.get("new")][:20],
                          ensure_ascii=False, sort_keys=True)
    return (
        f"被测函数：{target.get('function')}（{target.get('source_rel')}）\n\n"
        f"## 输入列（{len(table.inputs)} 个；输出列 {len(table.outputs)} 个，不可改）\n"
        f"{increment.columns_section(table, (check.get('input_ranges') or {}).get('contract'))}\n\n"
        f"## 义务语义证据（同一函数固定，未列出的控制关系不能猜测）\n{semantic_context}\n\n"
        f"{_gaps_section(check.get('completeness'), open_gaps)}\n\n"
        f"## 现有数据行（{len(table.rows)} 行）\n{increment.rows_section(table)}\n\n"
        f"## 当前版本的校验判定：{check.get('verdict')}\n{issues}\n\n"
        f"## 上轮新增行的程序检查反馈\n{feedback}\n\n"
        f"## 执行摘要\n{execution_summary}\n\n"
        f"## 已有版本的判定历史\n{rounds}"
    )


def _history_line(item: dict[str, Any]) -> str:
    line = f"- 第 {item['seq']} 版（{item['origin']}）：{item['verdict']} {item['codes']}"
    if item.get("added") is not None:
        line += f"；新增 {item['added']} 行，声明无法补齐 {item.get('declined', 0)} 个"
    for error in item.get("rejected") or ():
        line += f"；被拒：{error}"
    if item.get("witness_feedback"):
        line += "\n程序检查：" + _bounded_records(item["witness_feedback"], _MAX_DETAIL_CHARS)
        line += "\n该轮提议：" + _bounded_records(item.get("proposed_inputs") or [], _MAX_DETAIL_CHARS)
    return line


def _bounded_records(records: list[dict[str, Any]], limit: int) -> str:
    result = []
    for record in records:
        expanded = json.dumps(result + [record], ensure_ascii=False, sort_keys=True)
        if len(expanded) > limit:
            break
        result.append(record)
    return json.dumps(result, ensure_ascii=False, sort_keys=True)


def _declined(parsed: dict[str, Any], allowed: set[str]) -> list[str]:
    result = []
    for item in parsed.get("declined") or ():
        oid = str((item or {}).get("obligation") if isinstance(item, dict) else item or "").strip()
        if oid in allowed and oid not in result:
            result.append(oid)
    return result


def normalize_csv(text: str) -> bytes:
    """Mechanical transport fixes only: CRLF line endings and CP932 bytes."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n").split("\n")
    return ("\r\n".join(lines) + "\r\n").encode("cp932")


def revise(*, constraints: dict[str, Any], target: dict[str, Any], csv_bytes: bytes,
           check: dict[str, Any], execution: Optional[dict[str, Any]],
           history: list[dict[str, Any]], open_gaps: list[dict[str, Any]], allowed: set[str],
           model: str = "", timeout: int = 900) -> Revision:
    """Ask the configured model for input rows and append the valid ones.

    Raises :class:`provider.ProviderError` when the model is unreachable so the
    caller can stop without consuming a repair round.  A round without any
    accepted row comes back as ``Revision(csv_bytes=None)``; ``meta`` records
    accepted, rejected and declined obligations either way.
    """
    meta: dict[str, Any] = {"prompt_version": PROMPT_VERSION}
    try:
        table = increment.parse(csv_bytes.decode("cp932"))
    except (UnicodeDecodeError, increment.TableError) as exc:
        meta["error"] = f"当前 CSV 无法追加行：{exc}"
        return Revision(None, meta)
    messages = [
        {"role": "system", "content": _system_prompt(constraints)},
        {"role": "user", "content": _user_prompt(target, table, check, execution, history,
                                                 open_gaps)},
    ]
    meta["prompt_sha256"] = hashlib.sha256(
        json.dumps(messages, ensure_ascii=False).encode("utf-8")).hexdigest()
    usage: dict[str, int] = {}
    configured = ai_config.get_ai_config()[ai_config.KEY_MODEL]
    meta["model"] = model or configured
    reply = provider.invoke_chat(messages, temperature=0.1, max_tokens=8192,
                                 json_mode=True, usage=usage, model=model or None,
                                 timeout=timeout)
    meta["usage"] = usage
    try:
        parsed = provider.extract_json(reply)
        if not isinstance(parsed, dict):
            raise ValueError("reply is not a JSON object")
    except ValueError as exc:
        meta["error"] = f"模型输出不可用：{exc}"
        meta["reply_head"] = (reply or "")[:2000]
        return Revision(None, meta)
    meta["rationale"] = str(parsed.get("rationale") or "")
    applied = increment.apply(table, parsed.get("rows") or [], allowed,
                              (check.get("input_ranges") or {}).get("contract"))
    claimed = [row["obligation"] for row in applied.accepted]
    meta.update(rows=applied.accepted, rejected=applied.rejected, claimed=claimed,
                declined=[oid for oid in _declined(parsed, allowed) if oid not in claimed])
    meta["declined_reasons"] = [item for item in parsed.get("declined") or ()
                                if isinstance(item, dict) and item.get("obligation") in meta["declined"]]
    if applied.csv_bytes is None:
        reasons = "；".join(item["error"] for item in applied.rejected[:3])
        meta["error"] = ("模型未新增可用行" + (f"：{reasons}" if reasons else "")
                         + (f"；声明无法补齐 {len(meta['declined'])} 个" if meta["declined"] else ""))
    return Revision(applied.csv_bytes, meta)
