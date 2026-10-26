"""Installed configuration discovery never selects or mutates project semantics."""

from types import SimpleNamespace

import pytest

from app.services.lanmatrix.errors import ServiceError
from app.services.ut_pipeline import pipeline
from app.services.ut_pipeline.bridge import BridgeError, BridgeSettings, UtAgentBridge


def options():
    return {"contract": "ut-agent.configuration-options/1",
            "build_profiles": [{"id": "typed32", "label": "Typed 32-bit", "pointer_size": 4}],
            "interface_contracts": []}


def test_options_use_the_installed_bridge_without_a_manifest(monkeypatch):
    bridge = UtAgentBridge(BridgeSettings("", "", "", 7, 11, 13))
    calls = []

    def run(args, *, timeout):
        calls.append((args, timeout))
        return {"contract": "ut-agent.bridge/1", "ok": True, "configuration_options": options()}

    monkeypatch.setattr(bridge, "_run", run)
    monkeypatch.setattr(pipeline, "get_bridge", lambda: bridge)
    assert pipeline.manifest_options() == options()
    assert calls == [(["configuration-options"], 7)]


@pytest.mark.parametrize("result", [None, {}, {"contract": "other"},
                                   {**options(), "build_profiles": None}])
def test_malformed_discovery_is_a_visible_error_not_a_guessed_default(monkeypatch, result):
    monkeypatch.setattr(pipeline, "get_bridge", lambda: SimpleNamespace(configuration_options=lambda: result))
    with pytest.raises(ServiceError) as error:
        pipeline.manifest_options()
    assert error.value.code == "UT_CONFIGURATION_OPTIONS_UNAVAILABLE"


def test_old_or_unavailable_engine_has_an_actionable_error(monkeypatch):
    def unavailable():
        raise BridgeError("configuration-options command unavailable")

    monkeypatch.setattr(pipeline, "get_bridge", lambda: SimpleNamespace(configuration_options=unavailable))
    with pytest.raises(ServiceError) as error:
        pipeline.manifest_options()
    assert error.value.code == "UT_CONFIGURATION_OPTIONS_UNAVAILABLE"
    assert "ut-agent" in str(error.value) and "刷新" in str(error.value)
