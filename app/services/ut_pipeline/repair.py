"""Local-LLM revision of a TestCsv that failed the ut-agent check.

The model receives the constraint pack exported by ut-agent for the bound
baseline (the same rules the checker enforces), the input-completeness gaps
the generator reported for the target, the failing CSV and the machine
verdict.  Its output is only a candidate: it is executed and checked again by
ut-agent, and every outcome still ends at a human gate.

The model's job is input steps only.  Wrong or blank expected values are
settled by the platform with the WinAMS capture (see ``pipeline``), so the
model must keep existing expected values and leave new ones blank.

Prompt layout follows prefix-cache reuse on the local model: the system
message depends only on the batch constraint pack (shared by every function
of the batch), and the user message runs from the most to the least stable
part — target and its completeness gaps (fixed per job), current CSV, then
the verdict for that CSV, then the round history, which changes on every
round.  A round that kept the previous CSV therefore shares everything except
the history tail.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Optional

from ..ai import config as ai_config
from ..ai import provider

PROMPT_VERSION = "ut-repair/3.2"
# Bound the prompt: issue details are already truncated by ut-agent.
_MAX_DETAIL_CHARS = 6000
_MAX_DECISION_CHARS = 200
# Reported once in the gap section; repeating it in the verdict breaks nothing
# but costs prompt tokens on every round.
_GAP_CODE = "INPUT_OBLIGATION_UNRESOLVED"
# Only a missing case can be closed with input rows. Evidence gaps and
# unconfirmed infeasibility are counted for the model but never listed as work.
# Reports from before gap classes keep every gap as work.
_MISSING_CASE = "MISSING_CASE"


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
        "你是 WinAMS 单元测试用例（TestCsv）的修订助手。你只修订给定的 CSV，"
        "修订结果会被 ut-agent 校验器重新执行与判定，并由人工最终审核。\n\n"
        "## 硬性约束（校验器逐条检查，违反即判失败）\n"
        f"{revision_rules}\n\n"
        f"## 项目绑定的测试基准 {baseline.get('id')}@{baseline.get('version')}"
        f"（{source.get('document', '')} / {source.get('sheet', '')}）\n"
        f"{rules}\n\n策略参数：{policies}\n\n"
        f"## 覆盖要求\nC0: {coverage.get('c0')} / C1: {coverage.get('c1')} / "
        f"MC/DC: {coverage.get('mcdc')}（true 表示必须 100%）\n\n"
        "## 输出\n只输出一个 JSON 对象，不要输出其他文字：\n"
        '{"csv": "<修订后的完整 CSV 文本>", "rationale": "<修改依据>", '
        '"changes": [{"row": <新增数据行序号>, "obligation": "<义务 ID>", '
        '"inputs": {"<输入列名>": "<取值>"}, "reason": "<取值依据>"}]}\n\n'
        "## 修订方式\n"
        "- 你只负责输入手順：新增数据行来补齐输入完备性缺口或覆盖率缺口，不修改已有行。\n"
        "- 已有行的期望值不得改动；期望值与 WinAMS 实际值不一致时由平台用执行捕获值覆盖，不需要你处理。\n"
        "- 新增行的期望值单元格全部留空，由 WinAMS 执行捕获回填。\n"
        "- 每个新增行对应一个义务 ID，并依据判定式与列含义说明各输入取值；不要猜测，推不出的义务写进 rationale。\n"
        "- EXECUTION_PATH_DIVERGED 表示桩函数调用次数与预测冲突；可能来自输入手順、构建或运行配置，"
        "不能仅凭冲突认定缺少输入。只在给定语义证据支持时新增输入行并引用义务 ID；"
        "环境或构建原因无法排除时返回 csv:null，交由人工检查。\n"
        "- 所有缺口都无法从给定信息补齐时返回 {\"csv\": null, \"rationale\": \"<原因>\"}。"
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
    if item.get("reason"):
        line += f"：{item['reason']}"
    return line


def _completeness_section(completeness: Optional[dict[str, Any]]) -> str:
    if not completeness:
        return "## 输入完备性\n生成器未提供义务清单，按覆盖率与校验结果修订。"
    head = (f"Baseline 义务 {completeness.get('obligations')} 个，生成 {completeness.get('csv_rows')} 行，"
            f"按基准合并 {completeness.get('merged_by_baseline')} 个，"
            f"未解决 {completeness.get('unresolved_count')} 个。")
    unresolved = completeness.get("unresolved") or ()
    work = [item for item in unresolved if item.get("gap_class", _MISSING_CASE) == _MISSING_CASE]
    others = len(unresolved) - len(work)
    if others:
        head += (f"其中 {others} 个缺的是语义/环境证据或待人工确认不可达，"
                 "补输入行无法解决，已交由人工处理，不要为它们新增行。")
    gaps = "\n".join(_gap_line(item) for item in work)
    return ("## 输入完备性缺口（生成器没有给出已证明输入的义务）\n"
            f"{head}\n{gaps or '- 无'}")


def _user_prompt(target: dict[str, Any], csv_text: str, check: dict[str, Any],
                 execution: Optional[dict[str, Any]], history: list[dict[str, Any]]) -> str:
    issues = json.dumps(
        [issue for issue in check.get("issues", ()) if issue.get("code") != _GAP_CODE],
        ensure_ascii=False)[:_MAX_DETAIL_CHARS]
    execution_summary = json.dumps(
        {key: (execution or {}).get(key) for key in ("status", "exit_code", "diagnostic", "coverage")},
        ensure_ascii=False,
    )
    rounds = "\n".join(
        f"- 第 {item['seq']} 版（{item['origin']}）：{item['verdict']} {item['codes']}"
        for item in history
    ) or "- 无"
    return (
        f"被测函数：{target.get('function')}（{target.get('source_rel')}）\n\n"
        f"{_completeness_section(check.get('completeness'))}\n\n"
        f"## 当前 CSV\n```csv\n{csv_text}\n```\n\n"
        f"## 当前版本的校验判定：{check.get('verdict')}\n{issues}\n\n"
        f"## 执行摘要\n{execution_summary}\n\n"
        f"## 已有版本的判定历史\n{rounds}"
    )


def normalize_csv(text: str) -> bytes:
    """Mechanical transport fixes only: CRLF line endings and CP932 bytes."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n").split("\n")
    return ("\r\n".join(lines) + "\r\n").encode("cp932")


def revise(*, constraints: dict[str, Any], target: dict[str, Any], csv_bytes: bytes,
           check: dict[str, Any], execution: Optional[dict[str, Any]],
           history: list[dict[str, Any]], model: str = "", timeout: int = 900) -> Revision:
    """Ask the configured model for one revision.

    Raises :class:`provider.ProviderError` when the model is unreachable so the
    caller can stop without consuming a repair round.  An unusable reply is a
    consumed round and comes back as ``Revision(csv_bytes=None)``.
    """
    messages = [
        {"role": "system", "content": _system_prompt(constraints)},
        {"role": "user", "content": _user_prompt(
            target, csv_bytes.decode("cp932", "replace"), check, execution, history)},
    ]
    digest = hashlib.sha256(
        json.dumps(messages, ensure_ascii=False).encode("utf-8")).hexdigest()
    usage: dict[str, int] = {}
    configured = ai_config.get_ai_config()[ai_config.KEY_MODEL]
    meta: dict[str, Any] = {
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": digest,
        "model": model or configured,
    }
    reply = provider.invoke_chat(messages, temperature=0.1, max_tokens=16384,
                                 json_mode=True, usage=usage, model=model or None,
                                 timeout=timeout)
    meta["usage"] = usage
    try:
        parsed = provider.extract_json(reply)
        if not isinstance(parsed, dict):
            raise ValueError("reply is not a JSON object")
        meta["rationale"] = str(parsed.get("rationale") or "")
        changes = parsed.get("changes")
        meta["changes"] = changes if isinstance(changes, list) else []
        text = parsed.get("csv")
        if text is None:
            meta["error"] = "模型声明无法修复"
            return Revision(None, meta)
        if not isinstance(text, str) or not text.strip():
            raise ValueError("csv must be a non-empty string")
        return Revision(normalize_csv(text), meta)
    except (ValueError, UnicodeEncodeError) as exc:
        meta["error"] = f"模型输出不可用：{exc}"
        meta["reply_head"] = (reply or "")[:2000]
        return Revision(None, meta)
