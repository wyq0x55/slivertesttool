import json
import threading

import pytest

from test_ai_api import project_env
from test_ai_jobs import _draft


@pytest.fixture
def recovery_clock(monkeypatch):
    from app.services.ai import jobs, provider

    clock = [1000.0]
    monkeypatch.setattr(jobs, "_now", lambda: clock[0])
    monkeypatch.setattr(provider, "chat", lambda *args, **kwargs: pytest.fail("Unexpected provider call"))
    return clock


def _set_publication_lease(draft_id, until):
    from app.extensions import db
    from app.models import AiDraft
    from app.services.ai import jobs

    draft = db.session.get(AiDraft, draft_id)
    meta = jobs.metadata(draft)
    meta["job"]["publication_until"] = until
    draft.meta_json = json.dumps(meta)
    db.session.commit()


def test_periodic_recovery_preserves_live_slow_provider(app_ctx, project_env, recovery_clock, monkeypatch):
    from app.extensions import db
    from app.jobqueue import tasks
    from app.models import AiDraft
    from app.services.ai import jobs, provider

    monkeypatch.setattr(tasks, "_get_app", lambda: app_ctx)
    output = {"module_id": "M", "viewpoints": [{"case_id": "M-1", "kind": "normal",
              "title": "Valid", "expected": "Pass"}]}
    published = []

    def slow_provider(*args, **kwargs):
        recovery_clock[0] += jobs.LEASE_SECONDS + 1
        jobs.recover(published.append)
        return json.dumps(output)

    monkeypatch.setattr(provider, "chat", slow_provider)
    with app_ctx.app_context():
        draft_id, attempt = _draft(app_ctx, project_env)
        tasks.run_ai_generation(draft_id, attempt)
        draft = db.session.get(AiDraft, draft_id)
        assert draft.status == "pending"
        assert json.loads(draft.output_json) == output
        assert jobs.metadata(draft)["job"]["attempt"] == attempt
        assert published == []


def test_direct_claim_without_slot_remains_interrupted(app_ctx, project_env, recovery_clock):
    from app.extensions import db
    from app.models import AiDraft
    from app.services.ai import jobs

    with app_ctx.app_context():
        draft_id, attempt = _draft(app_ctx, project_env)
        assert jobs.claim(draft_id, attempt)
        recovery_clock[0] += jobs.LEASE_SECONDS + 1
        published = []
        assert jobs.recover(published.append) == 1
        assert published == [draft_id]
        assert jobs.metadata(db.session.get(AiDraft, draft_id))["job"]["attempt"] != attempt


def test_slot_release_drops_live_ownership_before_finish(app_ctx, project_env, recovery_clock):
    from app.extensions import db
    from app.models import AiDraft
    from app.services.ai import jobs

    with app_ctx.app_context():
        draft_id, attempt = _draft(app_ctx, project_env)
        assert jobs.acquire_slot()
        try:
            assert jobs.claim(draft_id, attempt)
        finally:
            jobs.release_slot()
        recovery_clock[0] += jobs.LEASE_SECONDS + 1
        assert jobs.recover(lambda identity: None) == 1
        assert jobs.metadata(db.session.get(AiDraft, draft_id))["job"]["attempt"] != attempt


def test_provider_base_exception_releases_slot_and_ownership(app_ctx, project_env, recovery_clock, monkeypatch):
    from app.jobqueue import tasks
    from app.services.ai import jobs, scenarios

    monkeypatch.setattr(tasks, "_get_app", lambda: app_ctx)

    def stopped(*args, **kwargs):
        raise SystemExit("provider stopped")

    monkeypatch.setattr(scenarios, "run_scenario", stopped)
    with app_ctx.app_context():
        draft_id, attempt = _draft(app_ctx, project_env)
        with pytest.raises(SystemExit, match="provider stopped"):
            tasks.run_ai_generation(draft_id, attempt)
        recovery_clock[0] += jobs.LEASE_SECONDS + 1
        assert jobs.recover(lambda identity: None) == 1
        assert jobs.acquire_slot()
        assert jobs.acquire_slot()
        try:
            assert not jobs.acquire_slot()
        finally:
            jobs.release_slot()
            jobs.release_slot()


def test_dead_owner_thread_releases_its_slot(app_ctx, project_env, recovery_clock):
    from app.services.ai import jobs

    errors = []
    with app_ctx.app_context():
        draft_id, attempt = _draft(app_ctx, project_env)

    def abandoned():
        try:
            with app_ctx.app_context():
                assert jobs.acquire_slot()
                assert jobs.claim(draft_id, attempt)
        except BaseException as exc:
            errors.append(exc)

    owner = threading.Thread(target=abandoned)
    owner.start()
    owner.join(timeout=5)
    assert not owner.is_alive() and errors == []
    with app_ctx.app_context():
        recovery_clock[0] += jobs.LEASE_SECONDS + 1
        assert jobs.recover(lambda identity: None) == 1
        acquired = [jobs.acquire_slot(), jobs.acquire_slot()]
        try:
            assert acquired == [True, True]
        finally:
            for held in acquired:
                if held:
                    jobs.release_slot()


def test_recovery_suppresses_queued_publication_until_lease_expires(app_ctx, project_env, recovery_clock):
    from app.extensions import db
    from app.models import AiDraft
    from app.services.ai import jobs

    with app_ctx.app_context():
        draft_id, attempt = _draft(app_ctx, project_env)
        _set_publication_lease(draft_id, recovery_clock[0] + jobs.LEASE_SECONDS)
        published = []
        for tick in range(10):
            assert jobs.recover(published.append) == 0
            recovery_clock[0] += 60
        assert published == []
        recovery_clock[0] = 1000.0 + jobs.LEASE_SECONDS
        assert jobs.recover(published.append) == 1
        assert published == [draft_id]
        assert jobs.metadata(db.session.get(AiDraft, draft_id))["job"]["attempt"] == attempt


def test_publish_once_deduplicates_and_retries_after_timeout(app_ctx, project_env, recovery_clock):
    from app.services.ai import jobs

    dispatched = []
    with app_ctx.app_context():
        draft_id, attempt = _draft(app_ctx, project_env)
        dispatch = lambda identity, token: dispatched.append((identity, token))
        assert jobs.publish_once(draft_id, dispatch)
        assert not jobs.publish_once(draft_id, dispatch)
        recovery_clock[0] += jobs.LEASE_SECONDS
        assert jobs.publish_once(draft_id, dispatch)
        assert dispatched == [(draft_id, attempt), (draft_id, attempt)]


def test_publication_failure_does_not_strand_other_drafts_or_retry(app_ctx, project_env, recovery_clock):
    from app.services.ai import jobs

    dispatched = []
    unavailable = [True]
    with app_ctx.app_context():
        failed_id, failed_attempt = _draft(app_ctx, project_env)
        healthy_id, healthy_attempt = _draft(app_ctx, project_env)

        def dispatch(identity, token):
            if identity == failed_id and unavailable[0]:
                raise ConnectionError("queue unavailable")
            dispatched.append((identity, token))

        def publish(identity):
            return jobs.publish_once(identity, dispatch)

        assert jobs.recover(publish) == 2
        assert dispatched == [(healthy_id, healthy_attempt)]
        unavailable[0] = False
        assert jobs.recover(publish) == 1
        assert dispatched == [(healthy_id, healthy_attempt), (failed_id, failed_attempt)]
        assert jobs.recover(publish) == 0


def test_late_publication_failure_cannot_clear_new_reservation(app_ctx, project_env, recovery_clock):
    from app.services.ai import jobs

    dispatched = []
    with app_ctx.app_context():
        draft_id, attempt = _draft(app_ctx, project_env)

        def late_failure(identity, token):
            recovery_clock[0] += jobs.LEASE_SECONDS
            assert jobs.publish_once(identity, lambda newer_id, newer_token: dispatched.append((newer_id, newer_token)))
            raise ConnectionError("late queue error")

        with pytest.raises(ConnectionError, match="late queue error"):
            jobs.publish_once(draft_id, late_failure)
        assert not jobs.publish_once(draft_id, lambda *args: pytest.fail("New reservation was cleared"))
        assert dispatched == [(draft_id, attempt)]


def test_publication_failure_cannot_clear_retried_attempt(app_ctx, project_env, recovery_clock):
    from app.extensions import db
    from app.models import AiDraft
    from app.services.ai import jobs

    dispatched = []
    with app_ctx.app_context():
        draft_id, old_attempt = _draft(app_ctx, project_env)

        def late_failure(identity, token):
            jobs.cancel(identity)
            jobs.retry(identity)
            assert jobs.publish_once(identity, lambda newer_id, newer_token: dispatched.append((newer_id, newer_token)))
            raise ConnectionError("old attempt error")

        with pytest.raises(ConnectionError, match="old attempt error"):
            jobs.publish_once(draft_id, late_failure)
        current = jobs.metadata(db.session.get(AiDraft, draft_id))["job"]["attempt"]
        assert current != old_attempt
        assert dispatched == [(draft_id, current)]
        assert jobs.recover(lambda identity: pytest.fail("New lease was cleared")) == 0


def test_startup_recovery_ignores_old_publication_lease(app_ctx, project_env, recovery_clock):
    from app.extensions import db
    from app.models import AiDraft
    from app.services.ai import jobs

    with app_ctx.app_context():
        draft_id, old_attempt = _draft(app_ctx, project_env)
        _set_publication_lease(draft_id, recovery_clock[0] + jobs.LEASE_SECONDS)
        dispatched = []
        publish = lambda identity: jobs.publish_once(identity, lambda newer_id, token: dispatched.append((newer_id, token)))
        assert jobs.recover(publish, startup=True) == 1
        current = jobs.metadata(db.session.get(AiDraft, draft_id))["job"]["attempt"]
        assert current != old_attempt
        assert dispatched == [(draft_id, current)]


def test_bounded_recovery_reaches_drafts_after_200_leased_predecessors(app_ctx, project_env, recovery_clock):
    from app.services.ai import jobs

    with app_ctx.app_context():
        for predecessor in range(200):
            draft_id, attempt = _draft(app_ctx, project_env)
            _set_publication_lease(draft_id, recovery_clock[0] + jobs.LEASE_SECONDS)
        last_id, last_attempt = _draft(app_ctx, project_env)
        published = []
        assert jobs.recover(published.append) == 0
        assert jobs.recover(published.append) == 1
        assert published == [last_id]
