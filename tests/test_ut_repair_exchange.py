"""Job-local model-exchange receipts; fake provider only, no database."""

import hashlib
import json
from pathlib import Path

import pytest

from app.services.ut_pipeline import repair
from test_ut_repair_prompt import CHECK, CONSTRAINTS, CSV, GAP


def _call(root=None):
    return repair.revise(constraints=CONSTRAINTS, target={"function": "f"}, csv_bytes=CSV.encode("cp932"),
                         check=CHECK, execution=None, history=[], open_gaps=[GAP],
                         allowed={GAP["obligation_id"]}, diagnostic_dir=root)


@pytest.fixture
def fake_model(monkeypatch):
    monkeypatch.setattr(repair.ai_config, "get_ai_config", lambda: {repair.ai_config.KEY_MODEL: "fake"})


@pytest.mark.parametrize("reply", [
    '{"rows":[{"obligation":"b6:boundary:1:above:2","base_row":1,"inputs":{"I2":"2"}}]}',
    '{"rows":[],"declined":[{"obligation":"b6:boundary:1:above:2","reason":"missing typed binding"}]}',
    '{"rows":[],"declined":1}',
    'not JSON\nraw response',
])
def test_receipts_bind_exact_actual_messages_and_raw_reply(tmp_path, monkeypatch, fake_model, reply):
    sent = []
    def invoke(messages, **kwargs):
        sent.append(messages)
        return reply
    monkeypatch.setattr(repair.provider, "invoke_chat", invoke)
    root = tmp_path / "job" / "a01-input-model-r01"
    result = _call(root)
    assert result.meta["artifact_root"] == str(root)
    assert json.loads((root / "request-messages.json").read_text(encoding="utf-8")) == sent[0]
    assert (root / "response.txt").read_bytes() == reply.encode("utf-8")
    assert {artifact["name"] for artifact in result.meta["artifacts"]} == {
        "request-messages.json", "response.txt"}
    for artifact in result.meta["artifacts"]:
        assert set(artifact) == {"id", "name", "sha256"}
        assert artifact["id"] == hashlib.sha256(artifact["name"].encode()).hexdigest()[:16]
        assert artifact["sha256"] == hashlib.sha256((root / artifact["name"]).read_bytes()).hexdigest()
    request_bytes = (root / "request-messages.json").read_bytes()
    assert result.meta["prompt_sha256"] == hashlib.sha256(request_bytes).hexdigest()


def test_provider_failure_preserves_request_and_propagates(tmp_path, monkeypatch, fake_model):
    def fail(*args, **kwargs):
        raise repair.provider.ProviderError("offline fake provider")
    monkeypatch.setattr(repair.provider, "invoke_chat", fail)
    root = tmp_path / "round"
    with pytest.raises(repair.provider.ProviderError):
        _call(root)
    messages = json.loads((root / "request-messages.json").read_text(encoding="utf-8"))
    assert [message["role"] for message in messages] == ["system", "user"]
    assert not (root / "response.txt").exists()


def test_existing_diagnostic_directory_is_not_overwritten(tmp_path, monkeypatch, fake_model):
    root = tmp_path / "round"
    root.mkdir()
    (root / "request-messages.json").write_bytes(b"prior request")
    monkeypatch.setattr(repair.provider, "invoke_chat",
                        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not call")))
    result = _call(root)
    assert result.csv_bytes is None and result.meta["code"] == "DIAGNOSTIC_WRITE_FAILED"
    assert result.meta["model_contacted"] is False
    assert (root / "request-messages.json").read_bytes() == b"prior request"


def test_response_write_failure_returns_no_candidate(tmp_path, monkeypatch, fake_model):
    save = repair.repair_exchange.save
    def fail_response(root, name, data, meta):
        if name == "response.txt":
            raise OSError("fake disk failure")
        save(root, name, data, meta)
    monkeypatch.setattr(repair.repair_exchange, "save", fail_response)
    monkeypatch.setattr(repair.provider, "invoke_chat", lambda *args, **kwargs:
                        '{"rows":[{"obligation":"b6:boundary:1:above:2","base_row":1,"inputs":{"I2":"2"}}]}')
    result = _call(tmp_path / "round")
    assert result.csv_bytes is None and result.meta["code"] == "DIAGNOSTIC_WRITE_FAILED"
    assert result.meta["model_contacted"] is True
    assert [artifact["name"] for artifact in result.meta["artifacts"]] == ["request-messages.json"]


def test_optional_diagnostics_does_not_create_directories(monkeypatch, fake_model):
    monkeypatch.setattr(Path, "mkdir", lambda *args, **kwargs:
                        (_ for _ in ()).throw(AssertionError("unexpected filesystem write")))
    monkeypatch.setattr(repair.provider, "invoke_chat", lambda *args, **kwargs: '{"rows":[]}')
    result = _call()
    assert "artifact_root" not in result.meta and "artifacts" not in result.meta
