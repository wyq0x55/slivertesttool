from __future__ import annotations

import asyncio
import io
import json
import os
import threading
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from unittest.mock import Mock

import pytest

from app.services.ai import base, config, jobs, provider, scenarios
from test_ai_api import _admin, _login, project_env


MESSAGES = [{"role": "user", "content": "private-prompt-marker"}]


def receipt(attempted=0, successful=0, api_responses=0):
    return {
        "schema_version": 1,
        "attempted_calls": attempted,
        "successful_calls": successful,
        "api_response_calls": api_responses,
    }


def http_reply(content="private-response-marker", **extra):
    payload = {"choices": [{"message": {"content": content}}], **extra}
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


@pytest.fixture(autouse=True)
def isolated_provider(monkeypatch, request):
    test_url = os.environ.get("TEST_DATABASE_URL")
    if "app_ctx" in request.fixturenames:
        assert test_url, "An explicit disposable TEST_DATABASE_URL is required"
    if test_url:
        monkeypatch.setenv("DATABASE_URL", test_url)
        monkeypatch.setenv("HUEY_DATABASE_URL", test_url)
    monkeypatch.setenv("LM_ALLOW_INSECURE_SECRET", "1")
    monkeypatch.setattr(config, "get_ai_config", lambda **kwargs: {
        config.KEY_API_BASE: "https://provider.invalid/v1",
        config.KEY_API_KEY: "private-credential-marker",
        config.KEY_MODEL: "configured-model-marker",
        config.KEY_TIMEOUT: 7,
    })
    blocked = Mock(side_effect=AssertionError("Unexpected unmocked HTTP call"))
    monkeypatch.setattr(provider.urllib.request, "urlopen", blocked)
    return blocked


def test_original_http_return_records_counters_only(monkeypatch):
    http = Mock(return_value=http_reply(usage={
        "prompt_tokens": 11, "completion_tokens": 5,
    }))
    monkeypatch.setattr(provider.urllib.request, "urlopen", http)
    usage = {}

    with provider.capture_provider_provenance() as captured:
        content = provider.invoke_chat(
            MESSAGES, temperature=0.4, max_tokens=20,
            json_mode=False, timeout=3, usage=usage,
        )

    assert content == "private-response-marker"
    assert usage == {"input_tokens": 11, "output_tokens": 5}
    assert captured == receipt(1, 1, 1)
    assert all(type(value) is int for value in captured.values())
    request = http.call_args.args[0]
    assert http.call_args.kwargs == {"timeout": 3}
    body = json.loads(request.data)
    assert body["temperature"] == 0.4
    assert body["max_tokens"] == 20
    assert "response_format" not in body


def test_chat_signature_and_keyword_only_fake_remain_compatible(monkeypatch):
    calls = []

    def fake_chat(messages, *, temperature=0.2, max_tokens=4096,
                  json_mode=True, timeout=None, usage=None):
        calls.append((messages, temperature, max_tokens, json_mode, timeout))
        if usage is not None:
            usage.update(input_tokens=2, output_tokens=1)
        return "content from patched chat"

    monkeypatch.setattr(provider, "chat", fake_chat)
    usage = {}
    with provider.capture_provider_provenance() as captured:
        content = provider.invoke_chat(
            MESSAGES, temperature=0.6, max_tokens=8,
            json_mode=False, timeout=2, usage=usage,
        )

    assert content == "content from patched chat"
    assert calls == [(MESSAGES, 0.6, 8, False, 2)]
    assert usage == {"input_tokens": 2, "output_tokens": 1}
    assert captured == receipt(1, 1, 0)


def test_mixed_original_and_patched_returns_cannot_look_all_real(monkeypatch):
    monkeypatch.setattr(provider.urllib.request, "urlopen",
                        Mock(return_value=http_reply()))
    with provider.capture_provider_provenance() as captured:
        provider.invoke_chat(MESSAGES)
        monkeypatch.setattr(provider, "chat", lambda *args, **kwargs: "fake")
        provider.invoke_chat(MESSAGES)

    assert captured == receipt(2, 2, 1)


def test_failed_http_attempt_then_reply_keeps_attempt_count(monkeypatch):
    failure = urllib.error.HTTPError(
        "https://provider.invalid/v1", 503, "private-error-marker", {},
        io.BytesIO(b"private-error-body-marker"),
    )
    monkeypatch.setattr(provider.urllib.request, "urlopen",
                        Mock(side_effect=[failure, http_reply()]))

    with provider.capture_provider_provenance() as captured:
        with pytest.raises(provider.ProviderError):
            provider.invoke_chat(MESSAGES)
        provider.invoke_chat(MESSAGES)

    assert captured == receipt(2, 1, 1)


@pytest.mark.parametrize("payload", [
    b"not-json", b'{}', b'{"choices": []}',
    b'{"choices": [{"message": null}]}',
])
def test_unparsed_http_reply_has_no_api_receipt(monkeypatch, payload):
    monkeypatch.setattr(provider.urllib.request, "urlopen",
                        Mock(return_value=io.BytesIO(payload)))
    with provider.capture_provider_provenance() as captured:
        with pytest.raises(provider.ProviderError):
            provider.invoke_chat(MESSAGES)

    assert captured == receipt(1, 0, 0)


@pytest.mark.parametrize("content", ["", None])
def test_empty_http_content_counts_as_a_returned_call(monkeypatch, content):
    monkeypatch.setattr(provider.urllib.request, "urlopen",
                        Mock(return_value=http_reply(content)))
    with provider.capture_provider_provenance() as captured:
        assert provider.invoke_chat(MESSAGES) == ""

    assert captured == receipt(1, 1, 1)


@pytest.mark.parametrize("content", ["", None])
def test_empty_patched_content_counts_as_a_returned_call(monkeypatch, content):
    monkeypatch.setattr(provider, "chat", lambda *args, **kwargs: content)
    with provider.capture_provider_provenance() as captured:
        assert provider.invoke_chat(MESSAGES) is content

    assert captured == receipt(1, 1, 0)


@pytest.mark.parametrize("content", ["", None])
def test_empty_http_then_nonempty_stub_cannot_look_all_real(monkeypatch, content):
    monkeypatch.setattr(provider.urllib.request, "urlopen",
                        Mock(return_value=http_reply(content)))
    with provider.capture_provider_provenance() as captured:
        assert provider.invoke_chat(MESSAGES) == ""
        monkeypatch.setattr(provider, "chat", lambda *args, **kwargs: '{"ok": true}')
        assert provider.invoke_chat(MESSAGES) == '{"ok": true}'

    assert captured == receipt(2, 2, 1)
    assert captured["successful_calls"] != captured["api_response_calls"]


def test_empty_http_then_actual_retry_keeps_all_return_counts(monkeypatch):
    monkeypatch.setattr(provider.urllib.request, "urlopen", Mock(side_effect=[
        http_reply(""), http_reply('{"ok": true}'),
    ]))
    with provider.capture_provider_provenance() as captured:
        result = base.generate_validated(
            build_prompt=lambda feedback: MESSAGES,
            validate=lambda parsed: [],
        )

    assert result.output == {"ok": True}
    assert result.rounds == 2
    assert captured == receipt(2, 2, 2)


def test_no_configuration_has_only_attempt_count(monkeypatch, isolated_provider):
    monkeypatch.setattr(config, "get_ai_config", lambda **kwargs: {
        config.KEY_API_BASE: "", config.KEY_API_KEY: "", config.KEY_MODEL: "",
    })
    with provider.capture_provider_provenance() as captured:
        with pytest.raises(provider.ProviderError):
            provider.invoke_chat(MESSAGES)

    assert captured == receipt(1, 0, 0)
    isolated_provider.assert_not_called()


def test_original_chat_alone_cannot_supply_success_or_attempt_count(monkeypatch):
    monkeypatch.setattr(provider.urllib.request, "urlopen",
                        Mock(return_value=http_reply()))
    with provider.capture_provider_provenance() as captured:
        assert provider.chat(MESSAGES) == "private-response-marker"

    assert captured == receipt(0, 0, 1)


def test_invoke_without_capture_preserves_return_and_exceptions(monkeypatch):
    monkeypatch.setattr(provider, "chat", Mock(return_value="unchanged"))
    assert provider.invoke_chat(MESSAGES) == "unchanged"
    failure = RuntimeError("unchanged failure")
    monkeypatch.setattr(provider, "chat", Mock(side_effect=failure))
    with pytest.raises(RuntimeError) as raised:
        provider.invoke_chat(MESSAGES)
    assert raised.value is failure


def test_nested_capture_restores_parent_even_on_exception(monkeypatch):
    monkeypatch.setattr(provider, "chat", lambda *args, **kwargs: "fake")
    with provider.capture_provider_provenance() as outer:
        provider.invoke_chat(MESSAGES)
        with pytest.raises(RuntimeError, match="nested failure"):
            with provider.capture_provider_provenance() as inner:
                provider.invoke_chat(MESSAGES)
                provider.invoke_chat(MESSAGES)
                raise RuntimeError("nested failure")
        provider.invoke_chat(MESSAGES)

    provider.invoke_chat(MESSAGES)
    with provider.capture_provider_provenance() as fresh:
        assert fresh == receipt()
    assert outer == receipt(2, 2, 0)
    assert inner == receipt(2, 2, 0)


def test_concurrent_threads_and_following_job_have_separate_counters(monkeypatch):
    monkeypatch.setattr(provider, "chat", lambda *args, **kwargs: "fake")
    barrier = threading.Barrier(2)

    def capture_calls(count):
        with provider.capture_provider_provenance() as captured:
            barrier.wait(timeout=5)
            for _call in range(count):
                provider.invoke_chat(MESSAGES)
        with provider.capture_provider_provenance() as following:
            provider.invoke_chat(MESSAGES)
        return captured, following

    with provider.capture_provider_provenance() as parent:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(capture_calls, 2)
            second = executor.submit(capture_calls, 3)
            assert first.result(timeout=10) == (receipt(2, 2, 0), receipt(1, 1, 0))
            assert second.result(timeout=10) == (receipt(3, 3, 0), receipt(1, 1, 0))
        assert parent == receipt()


def test_concurrent_async_contexts_do_not_mix_counters(monkeypatch):
    monkeypatch.setattr(provider, "chat", lambda *args, **kwargs: "fake")

    async def capture_calls(count):
        with provider.capture_provider_provenance() as captured:
            for _call in range(count):
                await asyncio.sleep(0)
                provider.invoke_chat(MESSAGES)
        return captured

    async def run():
        return await asyncio.gather(capture_calls(2), capture_calls(3))

    with provider.capture_provider_provenance() as parent:
        assert asyncio.run(run()) == [receipt(2, 2, 0), receipt(3, 3, 0)]
        assert parent == receipt()


def test_copied_context_capture_restores_its_parent(monkeypatch):
    monkeypatch.setattr(provider, "chat", lambda *args, **kwargs: "fake")

    def capture_child():
        with provider.capture_provider_provenance() as child:
            provider.invoke_chat(MESSAGES)
        return child

    with provider.capture_provider_provenance() as parent:
        assert copy_context().run(capture_child) == receipt(1, 1, 0)
        assert parent == receipt()
        provider.invoke_chat(MESSAGES)
    assert parent == receipt(1, 1, 0)


def test_validated_generator_includes_all_validation_retries(monkeypatch):
    http = Mock(side_effect=[http_reply("invalid-json"), http_reply('{"ok": true}')])
    monkeypatch.setattr(provider.urllib.request, "urlopen", http)
    with provider.capture_provider_provenance() as captured:
        result = base.generate_validated(
            build_prompt=lambda feedback: MESSAGES,
            validate=lambda parsed: [],
        )

    assert result.output == {"ok": True}
    assert result.rounds == 2
    assert captured == receipt(2, 2, 2)


def test_generator_keeps_minimal_chat_monkeypatch_keyword_contract(monkeypatch):
    def fake_chat(messages, *, temperature, max_tokens, usage):
        return '{"ok": true}'

    monkeypatch.setattr(provider, "chat", fake_chat)
    with provider.capture_provider_provenance() as captured:
        result = base.generate_validated(
            build_prompt=lambda feedback: MESSAGES,
            validate=lambda parsed: [],
        )

    assert result.output == {"ok": True}
    assert captured == receipt(1, 1, 0)


def test_procedure_capture_aggregates_plan_chunks_and_retry(monkeypatch):
    refs = [f"R{index}" for index in range(9)]
    plans = [{"ref": ref, "goal": {"IN": "1"}, "expected": {"OUT": "1"}}
             for ref in refs]

    def item(ref):
        return {"ref": ref, "steps": [{
            "no": 1, "inputs": {"IN": "1"}, "expecteds": {"OUT": "1"},
            "timing": "即時",
        }]}

    replies = [
        {"plans": []}, {"plans": plans}, {"procedures": []},
        {"procedures": [item(ref) for ref in refs[:8]]},
        {"procedures": [item(refs[8])]},
    ]
    http = Mock(side_effect=[http_reply(json.dumps(reply)) for reply in replies])
    monkeypatch.setattr(provider.urllib.request, "urlopen", http)
    with provider.capture_provider_provenance() as captured:
        result = scenarios.generate_procedure({
            "viewpoints": [{"ref": ref, "title": "Controlled viewpoint"}
                           for ref in refs],
            "sbs_variables": [["Input", "IN"], ["Output", "OUT"]],
        })

    assert result.output["failed_refs"] == []
    assert len(result.output["procedures"]) == 9
    assert captured == receipt(5, 5, 5)
    assert http.call_count == 5
    assert scenarios._STEP_CHUNK == 8
    assert base.DEFAULT_MAX_ROUNDS == 3


@pytest.mark.parametrize("mode, expected", [
    ("http", receipt(2, 2, 2)),
    ("patched", receipt(2, 2, 0)),
    ("mixed", receipt(2, 2, 1)),
    ("empty-http-then-patched", receipt(2, 2, 1)),
    ("replaced", receipt()),
    ("failed-then-http", receipt(2, 1, 1)),
])
def test_worker_persists_observed_receipt_for_controlled_generators(
        app_ctx, project_env, monkeypatch, mode, expected):
    from app.extensions import db
    from app.jobqueue import tasks
    from app.models import AiDraft

    monkeypatch.setattr(tasks, "_get_app", lambda: app_ctx)
    http = Mock(side_effect=lambda *args, **kwargs: http_reply("{}"))
    monkeypatch.setattr(provider.urllib.request, "urlopen", http)

    def generate(*args, **kwargs):
        if mode == "http":
            provider.invoke_chat(MESSAGES)
            provider.invoke_chat(MESSAGES)
        elif mode == "patched":
            with monkeypatch.context() as patch:
                patch.setattr(provider, "chat", lambda *args, **kwargs: "{}")
                provider.invoke_chat(MESSAGES)
                provider.invoke_chat(MESSAGES)
        elif mode == "mixed":
            provider.invoke_chat(MESSAGES)
            with monkeypatch.context() as patch:
                patch.setattr(provider, "chat", lambda *args, **kwargs: "{}")
                provider.invoke_chat(MESSAGES)
        elif mode == "empty-http-then-patched":
            with monkeypatch.context() as patch:
                patch.setattr(provider.urllib.request, "urlopen",
                              Mock(return_value=http_reply("")))
                provider.invoke_chat(MESSAGES)
            with monkeypatch.context() as patch:
                patch.setattr(provider, "chat", lambda *args, **kwargs: "{}")
                provider.invoke_chat(MESSAGES)
        elif mode == "failed-then-http":
            with monkeypatch.context() as patch:
                patch.setattr(provider.urllib.request, "urlopen", Mock(
                    side_effect=urllib.error.URLError("private-error-marker")))
                with pytest.raises(provider.ProviderError):
                    provider.invoke_chat(MESSAGES)
            provider.invoke_chat(MESSAGES)
        return base.GenerationResult(output={"ok": True}, model="configured-model-marker")

    monkeypatch.setattr(scenarios, "run_scenario", generate)
    with app_ctx.app_context():
        draft = AiDraft(project_id=project_env, scenario="viewpoint",
                        input_json="{}", status="running")
        attempt = jobs.prepare(draft)
        db.session.add(draft)
        db.session.commit()
        draft_id = draft.id
    with provider.capture_provider_provenance() as outer:
        tasks.run_ai_generation(draft_id, attempt)
        assert outer == receipt()
    with app_ctx.app_context():
        saved = db.session.get(AiDraft, draft_id)
        assert saved.status == "pending"
        assert jobs.metadata(saved)["provider_provenance"] == expected
        assert jobs.metadata(saved)["model"] == "configured-model-marker"


def test_worker_error_persists_attempts_and_cleans_up_capture(
        app_ctx, project_env, monkeypatch):
    from app.extensions import db
    from app.jobqueue import tasks
    from app.models import AiDraft

    monkeypatch.setattr(tasks, "_get_app", lambda: app_ctx)
    monkeypatch.setattr(provider.urllib.request, "urlopen", Mock(
        side_effect=urllib.error.URLError("private-error-marker")))
    monkeypatch.setattr(scenarios, "run_scenario",
                        lambda *args, **kwargs: provider.invoke_chat(MESSAGES))
    with app_ctx.app_context():
        draft = AiDraft(project_id=project_env, scenario="viewpoint", input_json="{}")
        attempt = jobs.prepare(draft)
        db.session.add(draft)
        db.session.commit()
        draft_id = draft.id
    tasks.run_ai_generation(draft_id, attempt)
    with app_ctx.app_context():
        saved = db.session.get(AiDraft, draft_id)
        assert saved.status == "error"
        assert jobs.metadata(saved)["provider_provenance"] == receipt(1, 0, 0)
    with provider.capture_provider_provenance() as fresh:
        assert fresh == receipt()


def test_concurrent_worker_jobs_keep_receipts_separate(
        app_ctx, project_env, monkeypatch):
    from app.extensions import db
    from app.jobqueue import tasks
    from app.models import AiDraft

    monkeypatch.setattr(tasks, "_get_app", lambda: app_ctx)
    monkeypatch.setattr(provider, "chat", lambda *args, **kwargs: "{}")
    barrier = threading.Barrier(2)

    def generate(scenario, payload, **kwargs):
        barrier.wait(timeout=5)
        for _call in range(payload["calls"]):
            provider.invoke_chat(MESSAGES)
        return base.GenerationResult(output={"ok": True})

    monkeypatch.setattr(scenarios, "run_scenario", generate)
    attempts = []
    with app_ctx.app_context():
        for count in (2, 3):
            draft = AiDraft(project_id=project_env, scenario="viewpoint",
                            input_json=json.dumps({"calls": count}))
            attempt = jobs.prepare(draft)
            db.session.add(draft)
            db.session.commit()
            attempts.append((draft.id, attempt))

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(tasks.run_ai_generation, draft_id, attempt)
                   for draft_id, attempt in attempts]
        for future in futures:
            future.result(timeout=10)

    with app_ctx.app_context():
        saved = [db.session.get(AiDraft, draft_id) for draft_id, attempt in attempts]
        assert [draft.status for draft in saved] == ["pending", "pending"]
        assert [jobs.metadata(draft)["provider_provenance"] for draft in saved] == [
            receipt(2, 2, 0), receipt(3, 3, 0),
        ]


def test_retry_clears_receipt_from_previous_attempt():
    from app.models import AiDraft

    draft = AiDraft(meta_json=json.dumps({
        "provider_provenance": receipt(1, 1, 1), "historical_note": "preserved",
    }))
    jobs.prepare(draft)
    assert "provider_provenance" not in jobs.metadata(draft)
    assert jobs.metadata(draft)["historical_note"] == "preserved"


def test_historical_draft_metadata_does_not_gain_a_receipt():
    from app.models import AiDraft

    draft = AiDraft(meta_json=json.dumps({"model": "configured-model-marker"}))
    assert jobs.metadata(draft) == {"model": "configured-model-marker"}


def test_finish_without_capture_cannot_infer_receipt_from_model(
        app_ctx, project_env):
    from app.extensions import db
    from app.models import AiDraft

    with app_ctx.app_context():
        draft = AiDraft(project_id=project_env, scenario="viewpoint", input_json="{}")
        attempt = jobs.prepare(draft)
        db.session.add(draft)
        db.session.commit()
        assert jobs.claim(draft.id, attempt)
        assert jobs.finish(draft.id, attempt, output={},
                           result_meta={"model": "configured-model-marker"})
        assert "provider_provenance" not in jobs.metadata(draft)


@pytest.mark.parametrize("stale", [False, True])
def test_finish_without_capture_discards_supplied_and_stale_receipts(app_ctx, project_env, stale):
    from app.extensions import db
    from app.models import AiDraft

    with app_ctx.app_context():
        draft = AiDraft(project_id=project_env, scenario="viewpoint", input_json="{}")
        attempt = jobs.prepare(draft)
        db.session.add(draft)
        db.session.commit()
        assert jobs.claim(draft.id, attempt)
        if stale:
            meta = jobs.metadata(draft)
            meta["provider_provenance"] = receipt(1, 1, 1)
            draft.meta_json = json.dumps(meta)
            db.session.commit()
        supplied = {"model": "configured-model-marker", "provider_provenance": receipt(1, 1, 1)}
        assert jobs.finish(draft.id, attempt, output={}, result_meta=supplied)
        assert "provider_provenance" not in jobs.metadata(draft)
        assert jobs.metadata(draft)["model"] == "configured-model-marker"
        assert supplied["provider_provenance"] == receipt(1, 1, 1)


@pytest.mark.parametrize("mode, expected", [
    ("http", receipt(1, 1, 1)),
    ("patched", receipt(1, 1, 0)),
    ("replaced", receipt()),
])
def test_immediate_route_generation_persists_worker_capture(
        client, app_ctx, project_env, monkeypatch, mode, expected):
    def generate(*args, **kwargs):
        if mode == "http":
            provider.invoke_chat(MESSAGES)
        elif mode == "patched":
            with monkeypatch.context() as patch:
                patch.setattr(provider, "chat", lambda *args, **kwargs: "{}")
                provider.invoke_chat(MESSAGES)
        return base.GenerationResult(output={"ok": True}, model="configured-model-marker")

    monkeypatch.setattr(provider.urllib.request, "urlopen",
                        Mock(side_effect=lambda *args, **kwargs: http_reply("{}")))
    monkeypatch.setattr(scenarios, "run_scenario", generate)
    response = client.post(
        "/api/v1/ai/drafts", headers=_login(client, _admin(client)),
        json={"scenario": "viewpoint", "project_id": project_env,
              "payload": {"doc_text": "Controlled requirement"}},
    )

    assert response.status_code == 201
    saved = response.get_json()["data"]
    assert saved["status"] == "pending"
    assert saved["meta"]["provider_provenance"] == expected
    detail = client.get(f"/api/v1/ai/drafts/{saved['id']}").get_json()["data"]
    assert detail["meta"]["provider_provenance"] == expected
