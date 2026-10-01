from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from flask import Flask
from werkzeug.datastructures import FileStorage


@pytest.fixture
def registry(tmp_path, monkeypatch):
    from app.services import project_model_service

    application = Flask(__name__)
    config = SimpleNamespace(MODEL_DIR=tmp_path / "models", RUNNER_BACKEND="mock")
    application.config_obj = config
    rows = []
    session = Mock()

    def add(row):
        row.id = len(rows) + 1
        rows.append(row)

    def select(_project_id, name, version=None, model_id=None):
        matches = [row for row in rows if row.name == name or row.id == model_id]
        if version:
            matches = [row for row in matches if row.version == version]
        return matches[-1] if matches else None

    session.add.side_effect = add
    monkeypatch.setattr(project_model_service, "db", SimpleNamespace(session=session))
    monkeypatch.setattr(project_model_service, "_models_root", lambda *_args: config.MODEL_DIR)
    monkeypatch.setattr(project_model_service, "_query", lambda *_args: SimpleNamespace(
        filter_by=lambda **kwargs: SimpleNamespace(
            all=lambda: [row for row in rows if row.name == kwargs["name"]],
        ),
    ))
    monkeypatch.setattr(project_model_service, "_select_model", select)
    monkeypatch.setattr(project_model_service, "has_models", lambda *_args: bool(rows))
    monkeypatch.setattr(project_model_service, "_audit_version_change", Mock())
    with application.app_context():
        yield project_model_service, config


def register(registry, tmp_path, kind, name, version, payload):
    service, config = registry
    if kind == "path":
        source = tmp_path / "source"
        source.mkdir(exist_ok=True)
        for suffix in ("sil", "dll", "sbs", "pdb"):
            (source / f"host.{suffix}").write_bytes(payload)
        return service.add_path_model(1, name, str(source / "host.sil"), version=version)
    uploads = {
        suffix: FileStorage(io.BytesIO(payload), filename=f"host.{suffix}")
        for suffix in ("dll", "sbs", "pdb")
    }
    return service.add_bundle_model(
        1, name, uploads["dll"], uploads["sbs"], config,
        pdb=uploads["pdb"], version=version,
    )


@pytest.mark.parametrize("kind", ["path", "bundle"])
@pytest.mark.parametrize("identities", [
    (("engine", ""), ("engine", "unversioned")),
    (("engine__v1", "v2"), ("engine", "v1__v2")),
])
def test_registration_preserves_colliding_model_directories(registry, tmp_path, kind, identities):
    first = register(registry, tmp_path, kind, *identities[0], b"old")
    old_path = Path(first["path"])
    old_files = {path.name: path.read_bytes() for path in old_path.parent.iterdir()}

    second = register(registry, tmp_path, kind, *identities[1], b"new")

    assert old_path.parent != Path(second["path"]).parent
    assert {path.name: path.read_bytes() for path in old_path.parent.iterdir()} == old_files


@pytest.mark.parametrize("kind", ["path", "bundle"])
def test_relabel_and_reregister_preserves_the_original_copy(registry, tmp_path, kind):
    service, _config = registry
    first = register(registry, tmp_path, kind, "engine", "v1", b"old")
    old_path = Path(first["path"])
    old_files = {path.name: path.read_bytes() for path in old_path.parent.iterdir()}
    service.update_version(1, "engine", "v2", model_id=first["id"])

    second = register(registry, tmp_path, kind, "engine", "v1", b"new")

    assert old_path.parent != Path(second["path"]).parent
    assert {path.name: path.read_bytes() for path in old_path.parent.iterdir()} == old_files


@pytest.mark.parametrize("kind", ["path", "bundle"])
def test_registration_preserves_an_unregistered_existing_directory(registry, tmp_path, kind):
    _service, config = registry
    existing = config.MODEL_DIR / "engine__v1"
    existing.mkdir(parents=True)
    marker = existing / "marker.txt"
    marker.write_bytes(b"keep")

    entry = register(registry, tmp_path, kind, "engine", "v1", b"new")

    assert marker.read_bytes() == b"keep"
    assert Path(entry["path"]).parent != existing


def test_failed_bundle_generation_keeps_existing_copies(registry, tmp_path, monkeypatch):
    service, _config = registry
    first = register(registry, tmp_path, "bundle", "engine", "", b"old")
    old_path = Path(first["path"])
    old_files = {path.name: path.read_bytes() for path in old_path.parent.iterdir()}
    failed_dirs = []

    def fail_generation(_config, path, *_args):
        failed_dirs.append(path.parent)
        raise service.ModelError("generation failed")

    monkeypatch.setattr(service, "_generate_sil", fail_generation)

    with pytest.raises(service.ModelError, match="generation failed"):
        register(registry, tmp_path, "bundle", "engine", "unversioned", b"new")

    assert {path.name: path.read_bytes() for path in old_path.parent.iterdir()} == old_files
    assert failed_dirs[0] != old_path.parent
    assert not failed_dirs[0].exists()
