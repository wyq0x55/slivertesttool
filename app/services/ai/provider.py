"""Minimal OpenAI-compatible chat-completions client (stdlib only).

Uses ``urllib.request`` rather than ``requests`` so the platform gains zero
new dependencies — the rest of the stack is deliberately dependency-light
for offline / internal-network installs.

The client speaks the de-facto standard ``POST {api_base}/chat/completions``
protocol understood by OpenAI, GLM, DeepSeek, Qwen (DashScope compatible
mode), vLLM / Ollama local gateways, and most intranet LLM proxies.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, Optional

from . import config


class ProviderError(RuntimeError):
    """Provider unreachable / non-200 / unparseable response."""


def chat(messages: list[dict[str, str]], *,
         temperature: float = 0.2,
         max_tokens: int = 4096,
         json_mode: bool = True,
         timeout: Optional[int] = None) -> str:
    """Call the configured model once and return the assistant text.

    ``temperature`` defaults low: test-case generation wants determinism and
    consistency across projects, not creativity.
    """
    cfg = config.get_ai_config(include_secret=True)
    if not (cfg[config.KEY_API_BASE] and cfg[config.KEY_API_KEY]
            and cfg[config.KEY_MODEL]):
        raise ProviderError("AI 未配置：请先在系统设置中填写 api_base / api_key / model")
    url = cfg[config.KEY_API_BASE].rstrip("/") + "/chat/completions"
    body: dict[str, Any] = {
        "model": cfg[config.KEY_MODEL],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        # Honoured by OpenAI-compatible gateways; ignored gracefully otherwise.
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + cfg[config.KEY_API_KEY],
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
                req, timeout=timeout or cfg[config.KEY_TIMEOUT]) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:500]
        except Exception:  # noqa: BLE001
            pass
        raise ProviderError(f"AI 接口返回 HTTP {exc.code}：{detail}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ProviderError(f"AI 接口无法访问：{exc}") from exc
    try:
        return payload["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError(f"AI 响应格式异常：{json.dumps(payload)[:500]}") from exc


_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*|\s*```$")


def extract_json(text: str) -> Any:
    """Best-effort JSON extraction from a model reply.

    Models frequently wrap JSON in `````json ... ``` `` fences or pad it with
    apologies. Try, in order: the whole text, the largest fenced block, the
    first balanced ``{...}`` / ``[...]`` span.
    """
    if text is None:
        raise ValueError("empty model reply")
    candidate = text.strip()
    try:
        return json.loads(candidate)
    except ValueError:
        pass
    for match in re.findall(r"```(?:json)?\s*([\s\S]*?)```", candidate):
        try:
            return json.loads(match.strip())
        except ValueError:
            continue
    for opener, closer in (("{", "}"), ("[", "]")):
        start = candidate.find(opener)
        if start < 0:
            continue
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(candidate)):
            ch = candidate[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(candidate[start:i + 1])
                    except ValueError:
                        break
    raise ValueError("model reply contains no parsable JSON")
