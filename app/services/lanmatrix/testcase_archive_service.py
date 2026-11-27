"""Read saved LAN Matrix rows and package them for Silver."""

import io
import tempfile
import zipfile
from pathlib import Path

from ...models import TestItemRow
from ..run_evidence_service import EvidenceError
from ..run_validation_service import ConversionError
from . import legacy_judge_export, silver_json_export
from .errors import ServiceError


def _export_cases(rows, test_ids, row_uuids=()):
    selected = {test_id.strip() for test_id in test_ids if test_id.strip()}
    selected_uuids = {str(uuid).strip() for uuid in row_uuids if str(uuid).strip()}
    cases = {}
    for row in rows:
        test_id = silver_json_export.row_test_id(row)
        if selected_uuids:
            if (row.uuid or "") not in selected_uuids:
                continue
            cases.setdefault(row.uuid, row)
            continue
        if selected:
            if test_id not in selected:
                continue
        else:
            if str(row.get_field("item_created") or "").strip() in {"不要", "未作成", "-"}:
                continue
            raw = row.get_field("steps")
            body = silver_json_export._parse_json_field(raw)
            if not raw or body and body.get("steps") == []:
                continue
        key = (row.book_id, test_id.casefold()) if row.book_id else test_id.casefold()
        if key in cases:
            raise ServiceError(f"测试用例 ID 重复：{test_id}", code="AMBIGUOUS_TEST_ID")
        cases[key] = row
    missing = selected - {silver_json_export.row_test_id(row) for row in cases.values()}
    if missing:
        raise ServiceError(f"未找到测试用例：{', '.join(sorted(missing))}", code="TESTCASE_NOT_FOUND")
    if not cases:
        raise ServiceError("没有可导出的测试用例", code="NO_TESTCASES")
    return list(cases.values())


def _silver_json_zip(cases, constants, library):
    buffer = io.BytesIO()
    with tempfile.TemporaryDirectory(prefix="silver-export-") as directory:
        root = Path(directory).resolve()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            used = set()
            for row in cases:
                test_id = silver_json_export.row_test_id(row)
                relative = Path(test_id)
                if relative.as_posix() in used:
                    # Two books may legitimately share a test id; keep both
                    # cases addressable instead of overwriting one folder.
                    relative = Path(f"book_{row.book_id}") / test_id
                used.add(relative.as_posix())
                case_dir = root / relative
                try:
                    silver_json_export.materialise_run_dir(case_dir, row, constants, library)
                except (ConversionError, EvidenceError) as exc:
                    raise ServiceError(f"{test_id}: {exc}", code="SILVER_JSON_EXPORT") from exc
                for path in sorted(case_dir.iterdir()):
                    if path.is_file():
                        archive.write(path, path.relative_to(root).as_posix())
    buffer.seek(0)
    return buffer


def export_project(project, *, legacy, test_ids=(), row_uuids=()):
    sheets = {"test": [], "const": [], "lib": []}
    rows = (TestItemRow.query.filter_by(project_id=project.id, deleted_at=None)
            .filter(TestItemRow.sheet.in_(sheets))
            .order_by(TestItemRow.row_order, TestItemRow.id).all())
    for row in rows:
        sheets[row.sheet].append(row)
    cases = _export_cases(sheets["test"], test_ids, row_uuids)
    if legacy:
        return legacy_judge_export.build_legacy_zip(project.code, cases, sheets["const"], sheets["lib"])
    return _silver_json_zip(cases, sheets["const"], sheets["lib"])
