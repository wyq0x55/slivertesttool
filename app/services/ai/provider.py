"""Minimal OpenAI-compatible chat-completions client (stdlib only).

Uses ``urllib.request`` rather than ``requests`` so the platform gains zero
new dependencies — the rest of the stack is deliberately dependency-light
for offline / internal-network installs.

The client speaks the de-facto standard ``POST {api_base}/chat/completions``
protocol understood by OpenAI, GLM, DeepSeek, Qwen (DashScope compatible
mode), vLLM / Ollama local gateways, and most intranet LLM proxies.
"""

from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Optional
from urllib.parse import urlparse

from . import config


class ProviderError(RuntimeError):
    """Provider unreachable / non-200 / unparseable response."""


_provenance: ContextVar[dict[str, int] | None] = ContextVar(
    "ai_provider_provenance", default=None)


@contextmanager
def capture_provider_provenance() -> Iterator[dict[str, int]]:
    """Capture counters for this execution and restore any enclosing capture."""
    counters = {"schema_version": 1, "attempted_calls": 0,
                "successful_calls": 0, "api_response_calls": 0}
    token = _provenance.set(counters)
    try:
        yield counters
    finally:
        _provenance.reset(token)


def get_provider_provenance() -> dict[str, int] | None:
    """Return a counters-only snapshot, or None outside a captured execution."""
    counters = _provenance.get()
    return dict(counters) if counters is not None else None


def invoke_chat(messages: list[dict[str, str]], **kwargs: Any) -> str:
    """Count attempts and non-throwing returns while preserving chat test fakes."""
    counters = _provenance.get()
    if counters is not None:
        counters["attempted_calls"] += 1
    content = chat(messages, **kwargs)
    if counters is not None:
        counters["successful_calls"] += 1
    return content


def chat(messages: list[dict[str, str]], *,
         temperature: float = 0.2,
         max_tokens: int = 4096,
         json_mode: bool = True,
         timeout: Optional[int] = None,
         usage: Optional[dict[str, int]] = None,
         model: Optional[str] = None,
         stream: bool = False) -> str:
    """Call the configured model once and return the assistant text.

    ``temperature`` defaults low: test-case generation wants determinism and
    consistency across projects, not creativity.

    ``model`` overrides the configured model name on the same gateway (e.g. a
    project that repairs with a specific local model).

    ``usage`` is an optional out-parameter: when a dict is passed, the
    gateway-reported token counts are written into it as
    ``input_tokens`` / ``output_tokens`` (0 when the gateway omits them).
    When the gateway reports prompt-cache details, the reused prefix length
    is added as ``cached_input_tokens``.
    An out-parameter rather than a richer return type keeps the contract
    a plain ``str`` — test fakes that patch this function stay valid.
    """
    cfg = config.get_ai_config(include_secret=True)
    if not (cfg[config.KEY_API_BASE] and cfg[config.KEY_API_KEY]
            and cfg[config.KEY_MODEL]):
        raise ProviderError("AI 未配置：请先在系统设置中填写 api_base / api_key / model")
    url = cfg[config.KEY_API_BASE].rstrip("/") + "/chat/completions"
    body: dict[str, Any] = {
        "model": model or cfg[config.KEY_MODEL],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        # Honoured by OpenAI-compatible gateways; ignored gracefully otherwise.
        body["response_format"] = {"type": "json_object"}
    if stream:
        body.update(stream=True, stream_options={"include_usage": True})
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + cfg[config.KEY_API_KEY],
        },
        method="POST",
    )
    hostname = (urlparse(url).hostname or "").lower()
    is_local = (
        hostname in ("127.0.0.1", "localhost", "::1")
        or hostname.startswith("10.")
        or hostname.startswith("192.168.")
        or (hostname.startswith("172.") and len(hostname.split(".")) > 1
            and hostname.split(".")[1].isdigit() and 16 <= int(hostname.split(".")[1]) <= 31)
    )
    # Local and intranet models must not be routed through the system proxy;
    # every other endpoint keeps the default ``urlopen`` handling.
    open_url = (urllib.request.build_opener(urllib.request.ProxyHandler({})).open
                if is_local else urllib.request.urlopen)
    started = time.monotonic()
    try:
        with open_url(
                req, timeout=timeout or cfg[config.KEY_TIMEOUT]) as resp:
            payload = (_stream_payload(resp, usage, started) if stream else
                       json.loads(resp.read().decode("utf-8")))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:500]
        except Exception:  # noqa: BLE001
            pass
        raise ProviderError(f"AI 接口返回 HTTP {exc.code}：{detail}") from exc
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        raise ProviderError(f"AI 接口无法访问：{exc}") from exc
    try:
        content = payload["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError(f"AI 响应格式异常：{json.dumps(payload)[:500]}") from exc
    if usage is not None:
        if stream:
            usage["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        reported = payload.get("usage") or {}
        usage["input_tokens"] = int(reported.get("prompt_tokens") or 0)
        usage["output_tokens"] = int(reported.get("completion_tokens") or 0)
        details = reported.get("prompt_tokens_details")
        if isinstance(details, dict) and "cached_tokens" in details:
            usage["cached_input_tokens"] = int(details.get("cached_tokens") or 0)
    counters = _provenance.get()
    if counters is not None:
        counters["api_response_calls"] += 1
    return content


def _stream_payload(response, usage: Optional[dict[str, int]], started: float) -> dict[str, Any]:
    """Collect complete SSE events; a dropped connection is never a reply."""
    fragments, reported, event = [], {}, []
    finished = False
    completed = False
    event_name = ""

    def consume(lines):
        nonlocal reported, finished, completed
        if event_name not in {"", "message", "chat.completion.chunk"}:
            raise ProviderError("AI 流式接口返回错误或未知事件")
        if not lines:
            return
        data = "\n".join(lines)
        if data == "[DONE]":
            if not completed:
                raise ProviderError("AI 流式响应缺少成功结束状态")
            finished = True
            return
        chunk = json.loads(data)
        if not isinstance(chunk, dict):
            raise ProviderError("AI 流式响应格式异常")
        if chunk.get("error"):
            raise ProviderError("AI 流式接口返回错误")
        if usage is not None and "first_event_ms" not in usage:
            usage["first_event_ms"] = round((time.monotonic() - started) * 1000)
        if isinstance(chunk.get("usage"), dict):
            reported = chunk["usage"]
        choices = chunk.get("choices") or []
        if not isinstance(choices, list):
            raise ProviderError("AI 流式选项格式异常")
        for choice in choices:
            if not isinstance(choice, dict) or not isinstance(choice.get("delta") or {}, dict):
                raise ProviderError("AI 流式选项格式异常")
            if choice.get("index", 0) != 0:
                continue
            text = (choice.get("delta") or {}).get("content")
            if text:
                if completed:
                    raise ProviderError("AI 流式响应在完成后继续输出文本")
                if not isinstance(text, str):
                    raise ProviderError("AI 流式文本格式异常")
                if usage is not None and "first_text_ms" not in usage:
                    usage["first_text_ms"] = round((time.monotonic() - started) * 1000)
                fragments.append(text)
            reason = choice.get("finish_reason")
            if reason is not None and reason != "stop":
                raise ProviderError("AI 流式输出未完成，无法作为修订结果")
            if reason == "stop":
                completed = True

    for raw in response:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            consume(event)
            event = []
            event_name = ""
            if finished:
                break
        elif line.startswith("data:"):
            event.append(line[5:].lstrip(" "))
        elif line.startswith("event:"):
            event_name = line[6:].strip()
    if event:
        consume(event)
    if not finished:
        raise ProviderError("AI 流式连接提前结束，未收到完成标记")
    return {"choices": [{"message": {"content": "".join(fragments)}}], "usage": reported}


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
