"""Per-project ``.sil`` plant models.

Model management lives inside a project: every project owns its own list of
Silver plant models. A model is registered in one of two ways:

* **Path** -- an administrator/editor registers an absolute server-side
  ``.sil`` path (``add_path_model``). The file is opened in place, exactly like
  the legacy global model registry.
* **Bundle** -- the user uploads a ``host.dll`` + ``host.sbs`` + ``host.pdb``
  set through the web UI (``add_bundle_model``). The service stores the files
  in a per-project directory and generates a fresh ``.sil`` through the real
  Silver API: an empty configuration is created in memory
  (``LocalSilverNative(sil=None)``), the module line ``<dll> -S <sbs>`` is
  injected with ``add_module`` and the result is written with ``save()`` (see
  :meth:`app.runners.silver_runner.SilverRunner.generate_sil`). The module line
  references the dll/sbs by their path relative to the Silver working directory
  (e.g. ``instance/model/project_1/host/host.dll``); the dll, sbs and pdb sit
  in that same directory so Silver can load the module and its debug symbols.
  When the mock runner is active (no license) a minimal text ``.sil`` is
  written instead.

Models are stored in the ``lm_project_models`` table (see
:class:`app.models.lanmatrix.ProjectModel`). For backward compatibility with
older single-model deployments, the ``effective_*`` helpers fall back to the
global admin registry (:mod:`.model_service`) when a project has no models of
its own.
"""

from __future__ import annotations

import datetime as _dt
import re
import shutil
from pathlib import Path
from typing import List, Optional

from werkzeug.datastructures import FileStorage

from ..config import BASE_DIR
from ..extensions import db
from ..models import Project, ProjectModel
from . import model_service


class ModelError(Exception):
    """Raised for invalid model registrations."""


# Characters unsafe as a single path segment / filename (mirrors run_layout).
_BAD_SEGMENT = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def _safe_segment(name: str, fallback: str) -> str:
    token = _BAD_SEGMENT.sub("_", (name or "").strip()).strip(" .")
    return token or fallback


def _safe_filename(name: str, fallback: str) -> str:
    """A safe base filename that preserves the extension."""
    base = Path((name or "").replace("\\", "/")).name
    token = _BAD_SEGMENT.sub("_", base).strip(" .")
    return token or fallback


# Global (silver-level) properties injected into every ``.sil`` generated from
# an uploaded dll/sbs bundle. macroStep is the macro time step; speedup fixes the
# real-time factor. Ordered ``(name, value)``; the value is the exact string
# written to the top-level ``<property .../>`` element.
_SIL_GLOBAL_PROPERTIES: list[tuple[str, str]] = [
    ("macroStep", "0.001"),
    ("speedup", "0.0"),
]


# --------------------------------------------------------------------------- #
# Low-level helpers
# --------------------------------------------------------------------------- #
def _query(project_id: int):
    return ProjectModel.query.filter_by(project_id=project_id)


def _legacy_segment(project_id: int) -> str:
    """The old id-based directory segment (``project_{id}``)."""
    return f"project_{project_id}"


def _project_segment(project_id: int) -> str:
    """Directory segment for a project's model bundles.

    Uses the project **code** (unique and immutable — it cannot be changed
    after creation, unlike ``name``) so the folder is human-readable
    (``instance/model/ISUZU/...`` rather than ``instance/model/project_2/...``)
    while staying stable and collision-free. Falls back to the legacy
    ``project_{id}`` segment when the project or its code is unavailable.
    """
    proj = Project.query.get(project_id) if project_id else None
    code = getattr(proj, "code", None) if proj is not None else None
    return _safe_segment(code or "", _legacy_segment(project_id))


def _models_root(config, project_id: int) -> Path:
    """Server directory that holds a project's uploaded model bundles."""
    return Path(config.MODEL_DIR) / _project_segment(project_id)


def _module_ref(file_path: Path) -> str:
    """The reference string written into the module line for *file_path*.

    Silver resolves module paths against its working directory (the app root),
    not against the ``.sil`` file, so the module line must carry the dll/sbs
    path relative to :data:`BASE_DIR` (e.g.
    ``instance/model/project_1/host/host.dll``). If the file lives outside the
    app root (a custom absolute ``MODEL_DIR``) the absolute path is used.
    """
    file_path = Path(file_path).resolve()
    try:
        return file_path.relative_to(Path(BASE_DIR).resolve()).as_posix()
    except ValueError:
        return file_path.as_posix()


def _write_text_sil(sil_path: Path, dll_ref: str, sbs_ref: str) -> None:
    """Mock-backend fallback: write a minimal text ``.sil`` (no license).

    Used only when the real Silver backend is unavailable (``mock`` runner /
    machines without a license). The file is never loaded by a real Silver
    instance in that mode, so a plain single-module line is sufficient.
    """
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    props = "".join(
        f'<property name="{name}" value="{value}"/>\n'
        for name, value in _SIL_GLOBAL_PROPERTIES)
    content = (
        "# Auto-generated by Silver Test Platform (mock backend)\n"
        f"# {stamp}\n"
        f"{dll_ref} -S {sbs_ref}\n"
        f"{props}"
    )
    sil_path.write_text(content, encoding="utf-8")


def _generate_sil(config, sil_path: Path, dll_ref: str, sbs_ref: str) -> None:
    """Generate the bundle's ``.sil``.

    ``dll_ref`` / ``sbs_ref`` are the module-line paths (relative to the Silver
    working directory). With the real Silver backend the file is produced
    through the Silver API (create an empty config, ``add_module``, ``save()``)
    so it is a genuine, loadable Silver model. With the mock backend a minimal
    text ``.sil`` is written instead so model creation still works without a
    license.
    """
    backend = getattr(config, "RUNNER_BACKEND", "mock")
    if backend == "silver":
        from ..runners.silver_runner import RunnerError, SilverRunner
        try:
            SilverRunner().generate_sil(sil_path, dll_ref, sbs_ref, index=3)
        except RunnerError as exc:
            raise ModelError(f"生成 .sil 失败：{exc}") from exc
        except Exception as exc:  # noqa: BLE001 - surface a clean 400 to the UI
            raise ModelError(
                f"生成 .sil 失败：{type(exc).__name__}: {exc}") from exc
    else:
        _write_text_sil(sil_path, dll_ref, sbs_ref)


# --------------------------------------------------------------------------- #
# Per-project reads
# --------------------------------------------------------------------------- #
def list_models(project_id: int, include_path: bool = False) -> List[dict]:
    rows = _query(project_id).order_by(ProjectModel.id.asc()).all()
    return [r.to_dict(include_path=include_path) for r in rows]


def has_models(project_id: int) -> bool:
    return db.session.query(ProjectModel.id).filter_by(
        project_id=project_id).first() is not None


def get_model_path(project_id: int, name: str) -> Optional[Path]:
    if not name:
        return None
    row = _query(project_id).filter_by(name=name).first()
    if row is not None and row.sil_path:
        return Path(row.sil_path)
    return None


def default_model(project_id: int) -> Optional[dict]:
    """The model a project runs tests with by default.

    The user-selected *current* model wins; if none is flagged (older data), the
    oldest model is used so a project with any model always resolves a default.
    """
    row = (_query(project_id).filter_by(is_current=True)
           .order_by(ProjectModel.id.asc()).first())
    if row is None:
        row = _query(project_id).order_by(ProjectModel.id.asc()).first()
    return {"name": row.name} if row is not None else None


def set_current(project_id: int, name: str) -> List[dict]:
    """Mark *name* as the project's current model (clearing any previous one).

    Exactly one row per project ends up current. Raises :class:`ModelError` if no
    model with that name exists in the project.
    """
    name = (name or "").strip()
    target = _query(project_id).filter_by(name=name).first() if name else None
    if target is None:
        raise ModelError("未找到该模型，请刷新后重试。")
    for row in _query(project_id).all():
        row.is_current = (row.id == target.id)
    db.session.commit()
    return list_models(project_id, include_path=True)


# --------------------------------------------------------------------------- #
# Effective reads (project models, falling back to the legacy global registry)
# --------------------------------------------------------------------------- #
def effective_models(project_id: int, include_path: bool = False) -> List[dict]:
    rows = list_models(project_id, include_path=include_path)
    if rows:
        return rows
    return model_service.list_models(include_path=include_path)


def effective_has(project_id: int) -> bool:
    return has_models(project_id) or model_service.has_models()


def effective_path(project_id: int, name: str) -> Optional[Path]:
    path = get_model_path(project_id, name)
    if path is not None:
        return path
    if not has_models(project_id):
        return model_service.get_model_path(name)
    return None


def effective_default(project_id: int) -> Optional[dict]:
    row = default_model(project_id)
    if row is not None:
        return row
    return model_service.default_model()


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #
def _validate_name(project_id: int, name: str) -> None:
    if _query(project_id).filter_by(name=name).first() is not None:
        raise ModelError(f"该项目已存在名为 '{name}' 的模型。")


def add_path_model(project_id: int, name: str, path: str,
                   created_by: Optional[int] = None) -> dict:
    """Register a server-side ``.sil`` path for a project."""
    name = (name or "").strip()
    path = (path or "").strip().strip('"')
    if not path:
        raise ModelError("请填写 .sil 文件的服务器绝对路径。")
    if not path.lower().endswith(".sil"):
        raise ModelError("路径必须指向 Silver 模型文件 (*.sil)。")
    if not name:
        name = Path(path).stem or Path(path).name
    _validate_name(project_id, name)
    first = not has_models(project_id)
    row = ProjectModel(project_id=project_id, name=name, kind="path",
                       sil_path=str(Path(path)), created_by=created_by,
                       is_current=first)
    db.session.add(row)
    db.session.commit()
    return row.to_dict(include_path=True)


def add_bundle_model(project_id: int, name: str, dll: FileStorage,
                     sbs: FileStorage, config,
                     pdb: Optional[FileStorage] = None,
                     created_by: Optional[int] = None) -> dict:
    """Store an uploaded ``dll`` + ``sbs`` (+ ``pdb``) set and generate the ``.sil``.

    A per-project model directory is created and all uploaded files are saved
    into it side by side. A fresh ``.sil`` is then generated through the Silver
    API (an empty config with a single ``<dll> -S <sbs>`` module, then
    ``save()``). The module line references the dll/sbs by their path relative
    to the Silver working directory (e.g.
    ``instance/model/project_1/host/host.dll``). The dll's matching ``.pdb``
    must sit in the same directory or Silver cannot locate the debug symbols.
    """
    name = (name or "").strip()
    if dll is None or not dll.filename:
        raise ModelError("请上传 dll 文件。")
    if sbs is None or not sbs.filename:
        raise ModelError("请上传 sbs 文件。")
    if pdb is None or not pdb.filename:
        raise ModelError("请上传 pdb 文件（否则 Silver 找不到调试符号）。")

    dll_name = _safe_filename(dll.filename, "host.dll")
    sbs_name = _safe_filename(sbs.filename, "host.sbs")
    pdb_name = _safe_filename(pdb.filename, "host.pdb")
    if not dll_name.lower().endswith(".dll"):
        raise ModelError("第一个文件必须是 .dll。")
    if not sbs_name.lower().endswith(".sbs"):
        raise ModelError("第二个文件必须是 .sbs。")
    if not pdb_name.lower().endswith(".pdb"):
        raise ModelError("第三个文件必须是 .pdb。")
    if not name:
        name = Path(dll_name).stem or "model"
    _validate_name(project_id, name)

    seg = _safe_segment(name, f"model_{Path(dll_name).stem}")
    model_dir = _models_root(config, project_id) / seg
    if model_dir.exists():
        shutil.rmtree(model_dir, ignore_errors=True)
    model_dir.mkdir(parents=True, exist_ok=True)

    try:
        dll.save(str(model_dir / dll_name))
        sbs.save(str(model_dir / sbs_name))
        pdb.save(str(model_dir / pdb_name))
        sil_path = model_dir / f"{seg}.sil"
        dll_ref = _module_ref(model_dir / dll_name)
        sbs_ref = _module_ref(model_dir / sbs_name)
        _generate_sil(config, sil_path, dll_ref, sbs_ref)
    except Exception:  # noqa: BLE001 - clean up a half-written bundle
        shutil.rmtree(model_dir, ignore_errors=True)
        raise

    first = not has_models(project_id)
    row = ProjectModel(project_id=project_id, name=name, kind="bundle",
                       sil_path=str(sil_path), bundle_dir=str(model_dir),
                       created_by=created_by, is_current=first)
    db.session.add(row)
    db.session.commit()
    return row.to_dict(include_path=True)


def remove_model(project_id: int, name: str) -> bool:
    """Remove a project model. Bundle files on disk are deleted too."""
    row = _query(project_id).filter_by(name=(name or "").strip()).first()
    if row is None:
        return False
    was_current = bool(row.is_current)
    if row.kind == "bundle" and row.bundle_dir:
        shutil.rmtree(row.bundle_dir, ignore_errors=True)
    db.session.delete(row)
    db.session.flush()
    # Deleting the current model promotes the oldest survivor so the project
    # keeps a well-defined model to run tests with.
    if was_current:
        nxt = _query(project_id).order_by(ProjectModel.id.asc()).first()
        if nxt is not None:
            nxt.is_current = True
    db.session.commit()
    return True

# --------------------------------------------------------------------------- #
# One-time migration: legacy ``project_{id}`` dirs -> code-based dirs
# --------------------------------------------------------------------------- #
def _remap_segment(path_str, old_seg, new_seg):
    """Replace the ``old_seg`` path component of *path_str* with *new_seg*."""
    if not path_str:
        return path_str
    parts = list(Path(path_str).parts)
    changed = False
    for i, part in enumerate(parts):
        if part == old_seg:
            parts[i] = new_seg
            changed = True
    return str(Path(*parts)) if changed else path_str


def _rewrite_sil_ref(sil_path, old_seg, new_seg):
    """Rewrite the dll/sbs module-line paths inside a generated ``.sil``.

    The module line carries the bundle paths relative to the Silver working
    directory (e.g. ``instance/model/project_2/host/host.dll``); after the
    directory is renamed the ``project_2`` component must become the code.
    Both real (XML) and mock (text) ``.sil`` files are plain text.
    """
    try:
        sp = Path(sil_path)
        if not sp.is_file():
            return
        txt = sp.read_text(encoding="utf-8")
        new = txt.replace("/" + old_seg + "/", "/" + new_seg + "/")
        if new != txt:
            sp.write_text(new, encoding="utf-8")
    except Exception:  # noqa: BLE001 - never block startup on a stray .sil
        pass


def migrate_model_dirs(config) -> int:
    """Rename legacy ``project_{id}`` model directories to code-based names.

    For every project whose code yields a directory segment different from the
    legacy ``project_{id}`` one, the on-disk bundle directory is moved and each
    bundle model's stored ``sil_path`` / ``bundle_dir`` (plus the ``.sil``
    module refs) are rewritten. Idempotent and defensive: safe to run on every
    start, never raises.
    """
    moved = 0
    try:
        root = Path(config.MODEL_DIR)
        projects = Project.query.all()
    except Exception:  # noqa: BLE001 - DB not ready / no projects
        return 0
    for proj in projects:
        old_seg = _legacy_segment(proj.id)
        new_seg = _safe_segment(getattr(proj, "code", "") or "", old_seg)
        if new_seg == old_seg:
            continue
        old_root = root / old_seg
        new_root = root / new_seg
        try:
            if old_root.exists() and old_root.resolve() != new_root.resolve():
                if new_root.exists():
                    continue  # target already there - stay safe, skip
                new_root.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old_root), str(new_root))
                moved += 1
        except Exception:  # noqa: BLE001
            continue
        for m in ProjectModel.query.filter_by(
                project_id=proj.id, kind="bundle").all():
            m.bundle_dir = _remap_segment(m.bundle_dir, old_seg, new_seg)
            m.sil_path = _remap_segment(m.sil_path, old_seg, new_seg)
            _rewrite_sil_ref(m.sil_path, old_seg, new_seg)
    try:
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
    return moved
