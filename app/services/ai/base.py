"""generate → validate → retry scaffold shared by all AI scenarios.

Every scenario follows the same loop: build a prompt, call the provider,
machine-validate the output, and on failure feed the validation errors back
for another round. Retries are capped; the machine-validation log is stored
on the draft so a human reviewer can see *why* the output is trusted.

A ``Validator`` returns ``[]`` when the output is acceptable, otherwise a
list of human-readable problems that get appended to the next round's prompt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from . import provider

Validator = Callable[[Any], list[str]]
PromptBuilder = Callable[[list[str]], list[dict[str, str]]]

DEFAULT_MAX_ROUNDS = 3


class GenerationError(RuntimeError):
    """Generation gave up after ``max_rounds`` rounds of failed validation."""


@dataclass
class GenerationResult:
    output: Any
    rounds: int = 1
    log: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""


def generate_validated(*,
                       build_prompt: PromptBuilder,
                       validate: Validator,
                       max_rounds: int = DEFAULT_MAX_ROUNDS,
                       temperature: float = 0.2,
                       max_tokens: int = 4096) -> GenerationResult:
    """Run the scenario loop and return the first accepted output.

    ``build_prompt(feedback)`` receives the previous round's validation
    problems (empty on round 1) and returns the chat messages.
    """
    feedback: list[str] = []
    log: list[dict[str, Any]] = []
    last_problems: list[str] = []
    for round_no in range(1, max_rounds + 1):
        messages = build_prompt(feedback)
        text = provider.chat(messages, temperature=temperature,
                             max_tokens=max_tokens)
        try:
            parsed = provider.extract_json(text)
        except ValueError as exc:
            problems = [f"输出不是合法 JSON：{exc}"]
        else:
            problems = _safe_validate(validate, parsed)
        log.append({"round": round_no,
                    "problems": problems,
                    "raw_chars": len(text)})
        if not problems:
            from . import config
            cfg = config.get_ai_config(include_secret=False)
            return GenerationResult(output=parsed, rounds=round_no, log=log,
                                    model=cfg.get(config.KEY_MODEL, ""))
        last_problems = problems
        feedback = problems
    raise GenerationError(
        "生成结果未通过校验（已重试 {} 轮）：{}".format(
            max_rounds, "; ".join(last_problems)))


def _safe_validate(validate: Validator, parsed: Any) -> list[str]:
    try:
        problems = validate(parsed)
    except Exception as exc:  # noqa: BLE001 - validator bugs must not loop forever
        return [f"校验器异常：{exc!r}"]
    if problems is None:
        return []
    return [str(p) for p in problems]


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)
