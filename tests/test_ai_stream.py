"""Transport evidence for the local repair client's streaming response."""
import io
import json
from unittest.mock import Mock

import pytest

from app.services.ai import config, provider


def _sse(*chunks, done=True):
    data = b"".join(("data: " + json.dumps(chunk) + "\r\n\r\n").encode("utf-8") for chunk in chunks)
    return io.BytesIO(data + (b"data: [DONE]\r\n\r\n" if done else b""))


def _delta(text, **other):
    return {"choices": [{"index": 0, "delta": {"content": text}, **other}]}


def test_stream_reconstructs_content_cache_usage_and_timing(monkeypatch):
    monkeypatch.setattr(config, "get_ai_config", lambda **_: {
        config.KEY_API_BASE: "https://model.invalid/v1", config.KEY_API_KEY: "test",
        config.KEY_MODEL: "test", config.KEY_TIMEOUT: 7})
    http = Mock(return_value=_sse(_delta('{"rows":'), _delta('[]}', finish_reason="stop"), {
        "choices": [], "usage": {"prompt_tokens": 2000, "completion_tokens": 10,
                                  "prompt_tokens_details": {"cached_tokens": 1700}}}))
    monkeypatch.setattr(provider.urllib.request, "urlopen", http)
    usage = {}
    assert provider.invoke_chat([{"role": "user", "content": "hi"}], stream=True, usage=usage) == '{"rows":[]}'
    body = json.loads(http.call_args.args[0].data)
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
    assert usage["cached_input_tokens"] == 1700 and usage["input_tokens"] == 2000
    assert 0 <= usage["first_event_ms"] <= usage["elapsed_ms"]
    assert 0 <= usage["first_text_ms"] <= usage["elapsed_ms"]


@pytest.mark.parametrize("response", [
    _sse(_delta('{"rows":[]}'), done=False),
    _sse(_delta("partial", finish_reason="length")),
    _sse({"error": {"message": "backend error"}}),
])
def test_incomplete_or_failed_stream_never_becomes_a_candidate(response):
    with pytest.raises(provider.ProviderError):
        provider._stream_payload(response, {}, 0)


def test_reasoning_events_are_not_mixed_into_the_json_reply():
    response = _sse({"choices": [{"delta": {"reasoning_content": "private reasoning"}}]}, _delta("{}", finish_reason="stop"))
    assert provider._stream_payload(response, {}, 0)["choices"][0]["message"]["content"] == "{}"


def test_stream_error_keeps_actionable_upstream_reason_and_redacts_credentials(monkeypatch):
    secret = "private-gateway-key"
    monkeypatch.setattr(config, "get_ai_config", lambda **_: {
        config.KEY_API_BASE: "https://model.invalid/v1", config.KEY_API_KEY: secret,
        config.KEY_MODEL: "test", config.KEY_TIMEOUT: 7})
    response = _sse({"error": {"type": "api_error", "code": 502,
        "message": f"connect 10.0.0.1:8095 timed out; {secret}; Bearer other-secret " + "x" * 1000}})
    monkeypatch.setattr(provider.urllib.request, "urlopen", Mock(return_value=response))
    with pytest.raises(provider.ProviderError) as failed:
        provider.invoke_chat([{"role": "user", "content": "hi"}], stream=True)
    message = str(failed.value)
    assert "api_error: 502: connect 10.0.0.1:8095 timed out" in message
    assert secret not in message and "other-secret" not in message
    assert len(message) < 650


@pytest.mark.parametrize("suffix", [
    b'event: error\ndata: {"message":"backend failed"}\n\n',
    b'data: {"choices":[{"delta":{},"finish_reason":"error"}]}\n\n',
])
def test_error_after_valid_json_is_not_success(suffix):
    prefix = _sse(_delta('{"rows":[]}', finish_reason="stop"), done=False).getvalue()
    response = io.BytesIO(prefix + suffix + b'data: [DONE]\n\n')
    with pytest.raises(provider.ProviderError):
        provider._stream_payload(response, {}, 0)


def test_done_marker_alone_does_not_prove_successful_completion():
    with pytest.raises(provider.ProviderError):
        provider._stream_payload(_sse(_delta('{"rows":[]}')), {}, 0)


@pytest.mark.parametrize("chunk", [[], {"choices": "bad"}, {"choices": [7]},
                                    {"choices": [{"delta": "bad"}]}])
def test_invalid_event_shape_is_a_provider_error(chunk):
    with pytest.raises(provider.ProviderError):
        provider._stream_payload(_sse(chunk), {}, 0)
