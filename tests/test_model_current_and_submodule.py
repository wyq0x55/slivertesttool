"""Current model selection and saved child-module dependencies."""

from io import BytesIO
from pathlib import Path

import pytest
from werkzeug.datastructures import FileStorage

from test_model_snapshot import app, _remote_model
from test_run_evidence import prepared


@pytest.fixture()
def project(app):
    from app.extensions import db
    from app.models import Project

    row = Project(code="CURRENT", name="Current model", owner_id=None)
    db.session.add(row)
    db.session.commit()
    return row


def upload(name, content=b"data"):
    return FileStorage(stream=BytesIO(content), filename=name)


def bundle(app, project, version="v1", sbs=b"host", extra=None):
    from app.services import project_model_service as models

    return models.add_bundle_model(
        project.id, "engine", upload("host.dll"), upload("host.sbs", sbs),
        app.config_obj, pdb=upload("host.pdb"), version=version, extra=extra)


def assert_current(project, expected):
    from app.models import ProjectModel

    assert [row.id for row in ProjectModel.query.filter_by(
        project_id=project.id, is_current=True).all()] == expected


@pytest.mark.parametrize("kind", ["path", "bundle"])
def test_new_registration_becomes_current(app, project, tmp_path, kind):
    from app.services import project_model_service as models

    sil = _remote_model(tmp_path)
    def register(version):
        if kind == "bundle":
            return bundle(app, project, version)
        return models.add_path_model(project.id, "engine", str(sil), version=version)

    first = register("v1")
    second = register("v2")
    assert_current(project, [second["id"]])
    assert models.resolve_ref(project.id, "")[0] == second["id"]
    assert models.resolve_ref(project.id, "engine@v1")[0] == first["id"]


def test_remove_current_promotes_newest_survivor(app, project):
    from app.services import project_model_service as models

    bundle(app, project, "v1")
    second = bundle(app, project, "v2")
    third = bundle(app, project, "v3")
    models.remove_model(project.id, "", model_id=third["id"])
    assert_current(project, [second["id"]])


def test_deprecate_current_promotes_newest_active_model(app, project):
    from app.services import project_model_service as models

    first, second, third = [bundle(app, project, version) for version in ("v1", "v2", "v3")]
    models.set_deprecated(project.id, "", True, model_id=second["id"])
    models.set_deprecated(project.id, "", True, model_id=third["id"])
    assert_current(project, [first["id"]])
    models.set_deprecated(project.id, "", True, model_id=first["id"])
    assert_current(project, [])
    assert models.default_model(project.id)["id"] == third["id"]


def test_default_fallback_prefers_newest_active_model(app, project):
    from app.extensions import db
    from app.models import ProjectModel
    from app.services import project_model_service as models

    bundle(app, project, "v1")
    second = bundle(app, project, "v2")
    third = bundle(app, project, "v3")
    models.set_deprecated(project.id, "", True, model_id=third["id"])
    for row in ProjectModel.query.filter_by(project_id=project.id):
        row.is_current = False
    db.session.commit()
    assert models.default_model(project.id)["id"] == second["id"]


def test_multiple_current_models_resolve_to_newest(app, project):
    from app.extensions import db
    from app.models import ProjectModel
    from app.services import project_model_service as models

    first = bundle(app, project, "v1")
    second = bundle(app, project, "v2")
    db.session.get(ProjectModel, first["id"]).is_current = True
    db.session.commit()
    assert models.default_model(project.id)["id"] == second["id"]


@pytest.mark.parametrize("operation", ["remove", "deprecate"])
def test_promotion_clears_other_current_rows(app, project, operation):
    from app.extensions import db
    from app.models import ProjectModel
    from app.services import project_model_service as models

    first, second, retired, current = [
        bundle(app, project, version) for version in ("v1", "v2", "v3", "v4")]
    models.set_deprecated(project.id, "", True, model_id=retired["id"])
    db.session.get(ProjectModel, first["id"]).is_current = True
    db.session.commit()
    if operation == "remove":
        models.remove_model(project.id, "", model_id=current["id"])
    else:
        models.set_deprecated(project.id, "", True, model_id=current["id"])
    assert_current(project, [second["id"]])


def assert_pinned_children(tmp_path, saved, child="dsp.dll", symbols=True):
    from app.services import run_evidence_service as evidence

    task, source, _ = prepared(tmp_path)
    task.sil_relpath = saved["path"]
    root = evidence.pin_attempt(task, source)
    for directory in (Path(saved["path"]).parent, root / "model"):
        assert (directory / child).read_bytes() == b"child"
        if symbols:
            assert (directory / Path(child).with_suffix(".pdb")).read_bytes() == b"symbols"


@pytest.mark.parametrize("child,quote", [("dsp.dll", ""), ("dsp.dll", '"'), ("children/dsp.dll", '"')])
def test_path_child_dependencies_are_saved_and_pinned(app, project, tmp_path, child, quote):
    from app.services import project_model_service as models

    sil = _remote_model(tmp_path)
    (sil.parent / "host.sbs").write_text(f"sub_module({quote}{child}{quote})", encoding="utf-8")
    dll = sil.parent / child
    dll.parent.mkdir(exist_ok=True)
    dll.write_bytes(b"child")
    dll.with_suffix(".pdb").write_bytes(b"symbols")
    saved = models.add_path_model(project.id, "engine", str(sil))
    assert_pinned_children(tmp_path, saved, child)


@pytest.mark.parametrize("prefix", ["//", "#", "*"])
def test_comment_submodule_is_ignored(app, project, tmp_path, prefix):
    from app.services import project_model_service as models
    from app.services import run_evidence_service as evidence

    sil = _remote_model(tmp_path)
    (sil.parent / "host.sbs").write_text(f"  {prefix} sub_module(missing.dll)", encoding="utf-8")
    saved = models.add_path_model(project.id, "engine", str(sil))
    task, source, _ = prepared(tmp_path)
    task.sil_relpath = saved["path"]
    evidence.pin_attempt(task, source)


def test_missing_bundle_child_leaves_no_model(app, project):
    from app.models import ProjectModel
    from app.services import project_model_service as models

    root = Path(app.config_obj.MODEL_DIR) / project.code
    before = set(root.glob("*"))
    with pytest.raises(models.ModelError, match=r"dsp.dll.*上传"):
        bundle(app, project, sbs=b"sub_module(dsp.dll)")
    assert ProjectModel.query.filter_by(project_id=project.id).count() == 0
    assert set(root.glob("*")) == before


@pytest.mark.parametrize("symbols", [True, False])
@pytest.mark.parametrize("child,quote", [("dsp.dll", ""), ("dsp.dll", '"'), ("children/dsp.dll", '"')])
def test_bundle_extra_children_are_saved_and_pinned(app, project, tmp_path, symbols, child, quote):
    extra = [upload("dsp.dll", b"child")]
    if symbols:
        extra.append(upload("dsp.pdb", b"symbols"))
    saved = bundle(app, project, sbs=f"sub_module({quote}{child}{quote})".encode(), extra=extra)
    assert_pinned_children(tmp_path, saved, child, symbols=symbols)


@pytest.mark.parametrize("refs", [
    ("children/dsp.dll", "other/dsp.dll"),
    ("dsp.dll", "children/dsp.dll"),
    ("children/dsp.dll", "dsp.dll"),
])
def test_bundle_rejects_child_filename_in_multiple_directories(app, project, refs):
    from app.models import ProjectModel
    from app.services import project_model_service as models

    root = Path(app.config_obj.MODEL_DIR) / project.code
    before = set(root.glob("*"))
    sbs = "\n".join(f'sub_module("{ref}")' for ref in refs).encode()
    with pytest.raises(models.ModelError, match="多个目录"):
        bundle(app, project, sbs=sbs, extra=[upload("dsp.dll"), upload("dsp.pdb")])
    assert ProjectModel.query.filter_by(project_id=project.id).count() == 0
    assert set(root.glob("*")) == before


@pytest.mark.parametrize("names", [("bad.txt",), ("host.dll",), ("dsp.dll", "DSP.dll")])
def test_invalid_bundle_extra_files_are_rejected(app, project, names):
    from app.models import ProjectModel
    from app.services import project_model_service as models

    with pytest.raises(models.ModelError):
        bundle(app, project, extra=[upload(name) for name in names])
    assert ProjectModel.query.filter_by(project_id=project.id).count() == 0


@pytest.mark.parametrize("child", ["missing.dll", "../outside.dll"])
def test_path_missing_or_escaping_child_is_rejected(app, project, tmp_path, child):
    from app.models import ProjectModel
    from app.services import project_model_service as models

    sil = _remote_model(tmp_path)
    (tmp_path / "outside.dll").write_bytes(b"outside")
    (sil.parent / "host.sbs").write_text(f"sub_module({child})", encoding="utf-8")
    root = Path(app.config_obj.MODEL_DIR) / project.code
    before = set(root.glob("*"))
    with pytest.raises(models.ModelError, match="dll"):
        models.add_path_model(project.id, "engine", str(sil))
    assert ProjectModel.query.filter_by(project_id=project.id).count() == 0
    assert set(root.glob("*")) == before


@pytest.mark.parametrize("child", ["dsp.dll", "../outside.dll"])
def test_pin_rejects_missing_or_escaping_saved_child(tmp_path, monkeypatch, child):
    from app import config
    from app.services import run_evidence_service as evidence

    task, source, sil = prepared(tmp_path)
    dll, sbs = sil.with_suffix(".dll"), sil.with_suffix(".sbs")
    dll.write_bytes(b"host")
    sbs.write_text(f"sub_module({child})", encoding="utf-8")
    sil.write_text(f"{dll.as_posix()} -S {sbs.as_posix()}", encoding="utf-8")
    # An unrelated application-root DLL must never satisfy a saved dependency.
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    (tmp_path / "dsp.dll").write_bytes(b"unrelated")
    (tmp_path / "outside.dll").write_bytes(b"outside")
    with pytest.raises(evidence.EvidenceError, match="missing: dsp.dll|containment"):
        evidence.pin_attempt(task, source)
