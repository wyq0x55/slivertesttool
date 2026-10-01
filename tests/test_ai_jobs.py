import json

import pytest

from test_ai_api import _admin, _login, project_env


def _draft(app_ctx, project_id):
    from app.extensions import db
    from app.models import AiDraft
    from app.services.ai import jobs

    draft = AiDraft(project_id=project_id, scenario="viewpoint", status="running",
                    input_json=json.dumps({"doc_text": "requirement"}))
    jobs.prepare(draft)
    db.session.add(draft)
    db.session.commit()
    return draft.id, jobs.metadata(draft)["job"]["attempt"]


def test_generation_claim_is_once_per_attempt(app_ctx, project_env):
    from app.services.ai import jobs

    with app_ctx.app_context():
        draft_id, attempt = _draft(app_ctx, project_env)
        assert jobs.claim(draft_id, attempt)
        assert not jobs.claim(draft_id, attempt)
        assert not jobs.claim(draft_id, "old-attempt")


def test_ai_slots_are_bounded_and_independent():
    from app.services.ai import jobs

    acquired = []
    try:
        for _index in range(3):
            acquired.append(jobs.acquire_slot())
        assert acquired == [True, True, False]
    finally:
        for held in acquired:
            if held:
                jobs.release_slot()


def test_cancel_cannot_be_overwritten_by_late_result(app_ctx, project_env):
    from app.extensions import db
    from app.models import AiDraft
    from app.services.ai import jobs

    with app_ctx.app_context():
        draft_id, attempt = _draft(app_ctx, project_env)
        assert jobs.claim(draft_id, attempt)
        jobs.cancel(draft_id)
        assert not jobs.finish(draft_id, attempt, output={"unsafe": True})
        draft = db.session.get(AiDraft, draft_id)
        assert draft.status == "cancelled"
        assert not draft.output_json


def test_recovery_rotates_attempt_without_changing_inputs(app_ctx, project_env):
    from app.extensions import db
    from app.models import AiDraft
    from app.services.ai import jobs

    with app_ctx.app_context():
        draft_id, old_attempt = _draft(app_ctx, project_env)
        jobs.claim(draft_id, old_attempt)
        original = db.session.get(AiDraft, draft_id).input_json
        published = []
        assert jobs.recover(published.append, startup=True) == 1
        assert published == [draft_id]
        draft = db.session.get(AiDraft, draft_id)
        assert jobs.metadata(draft)["job"]["attempt"] != old_attempt
        assert draft.input_json == original
        assert not jobs.finish(draft_id, old_attempt, output={"unsafe": True})


def test_retry_is_explicit_and_terminal_drafts_are_not_recovered(app_ctx, project_env):
    from app.extensions import db
    from app.models import AiDraft
    from app.services.ai import jobs

    with app_ctx.app_context():
        draft_id, attempt = _draft(app_ctx, project_env)
        jobs.claim(draft_id, attempt)
        jobs.finish(draft_id, attempt, error="provider unavailable")
        assert jobs.recover(lambda _draft_id: pytest.fail("must not replay failure"), startup=True) == 0
        jobs.retry(draft_id)
        assert db.session.get(AiDraft, draft_id).status == "running"
        assert jobs.metadata(db.session.get(AiDraft, draft_id))["job"]["attempt"] != attempt


def test_worker_preserves_cancel_and_catches_unexpected_error(app_ctx, project_env, monkeypatch):
    from app.extensions import db
    from app.models import AiDraft
    from app.services.ai import jobs, scenarios
    from app.services.ai.base import GenerationResult
    from app.jobqueue import tasks

    monkeypatch.setattr(tasks, "_get_app", lambda: app_ctx)
    with app_ctx.app_context():
        draft_id, attempt = _draft(app_ctx, project_env)

        def generate(*args, **kwargs):
            jobs.cancel(draft_id)
            return GenerationResult(output={"module_id": "M", "viewpoints": []})

        monkeypatch.setattr(scenarios, "run_scenario", generate)
        tasks.run_ai_generation(draft_id, attempt)
        assert db.session.get(AiDraft, draft_id).status == "cancelled"
        other_id, other_attempt = _draft(app_ctx, project_env)

        def broken(*args, **kwargs):
            raise RuntimeError("unexpected failure")

        monkeypatch.setattr(scenarios, "run_scenario", broken)
        tasks.run_ai_generation(other_id, other_attempt)
        assert db.session.get(AiDraft, other_id).status == "error"


def test_cancel_api_is_project_scoped_and_csrf_guarded(client, app_ctx, project_env):
    with app_ctx.app_context():
        draft_id, _attempt = _draft(app_ctx, project_env)
    headers = _login(client, _admin(client))
    response = client.post(f"/api/v1/ai/drafts/{draft_id}/cancel", json={})
    assert response.status_code == 403
    response = client.post(f"/api/v1/ai/drafts/{draft_id}/cancel", json={}, headers=headers)
    assert response.status_code == 200
    assert response.get_json()["data"]["status"] == "cancelled"


def test_failed_publication_stays_recoverable(app_ctx, project_env):
    from app.extensions import db
    from app.models import AiDraft
    from app.services.ai import jobs

    with app_ctx.app_context():
        draft_id, _attempt = _draft(app_ctx, project_env)

        def unavailable(_draft_id):
            raise ConnectionError("unavailable")

        jobs.recover(unavailable)
        draft = db.session.get(AiDraft, draft_id)
        assert draft.status == "running"
        assert jobs.metadata(draft)["job"]["state"] == "queued"
        published = []
        jobs.recover(published.append)
        assert published == [draft_id]
