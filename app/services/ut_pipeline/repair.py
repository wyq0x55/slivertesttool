"""Local-LLM revision of a TestCsv that failed the ut-agent check.

The model receives the constraint pack exported by ut-agent for the bound
baseline (the same rules the checker enforces), the failing CSV and the
machine verdict.  Its output is only a candidate: it is executed and checked
again by ut-agent, and every outcome still ends at a human gate.

Prompt layout follows prefix-cache reuse on the local model: the system
message depends only on the batch constraint pack (shared by every function
of the batch), and the user message runs from the most to the least stable
part — target and current CSV, then the verdict for that CSV, then the round
history, which changes on every round.  A round that kept the previous CSV
therefore shares everything except the history tail.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Optional

from ..ai import config as ai_config
from ..ai import provider

PROMPT_VERSION = "ut-repair/2"
# Bound the prompt: issue details are already truncated by ut-agent.
_MAX_DETAIL_CHARS = 6000


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
        '"changes": [{"row": <数据行序号>, "column": "<列名>", "before": "<原值>", '
        '"after": "<新值>", "reason": "<依据>"}]}\n'
        "期望值只能依据校验结果中的执行事实或明确的计算修改，并在 changes 中逐格说明；"
        "无法从给定信息修复时返回 {\"csv\": null, \"rationale\": \"<原因>\"}。"
    )


def _user_prompt(target: dict[str, Any], csv_text: str, check: dict[str, Any],
                 execution: Optional[dict[str, Any]], history: list[dict[str, Any]]) -> str:
    issues = json.dumps(check.get("issues", []), ensure_ascii=False)[:_MAX_DETAIL_CHARS]
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
