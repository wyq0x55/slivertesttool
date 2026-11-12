"""Build offline qualification reports for the complete project test matrix."""

from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime
from pathlib import Path

from ..models import TestItemRow
from . import report_service, run_evidence_service as evidence, task_service
from .lanmatrix.silver_json_export import row_test_id
from .qualification_report_renderer import render_reports

_PREFIX = re.compile(r"^(?:DEBUG|INFO|WARNING|ERROR|CRITICAL):[^:]*:\s?")
_START = re.compile(r"Test case ID\.(.+?)\s+is started!", re.I)
_END = re.compile(r"Test\s+is\s+(?:passed|failed)|All\s+steps\s+are\s+verified", re.I)
_DIVIDER = re.compile(r"^-{3,}\s*(.*?)\s*-{3,}$")
_VERDICT = re.compile(r"^(?:(.*?)\s+)?Step\.(\d+)\s+is\s+(passed|failed)(?:\s+at\s+([0-9.eE+-]+)s)?\.?$", re.I)
_SUBROUTINE = re.compile(r"^Subroutine\((.+)\)\s+is\s+(started|ended)!$", re.I)


def _case_section(text: str, test_id: str) -> str:
    """Select the latest exact case, stopping before foreign teardown output."""
    lines = [_PREFIX.sub("", line).strip() for line in text.splitlines()]
    starts = [(i, match.group(1).strip()) for i, line in enumerate(lines)
              if (match := _START.search(line))]
    if starts:
        matching = [i for i, identifier in starts
                    if identifier in (test_id, "ID;;" + test_id)]
        if not matching:
            return ""
        begin = matching[-1]
        end = next((i for i, _ in starts if i > begin), len(lines))
    else:
        begin, end = 0, len(lines)
    for i in range(begin, end):
        if _END.search(lines[i]):
            end = i + 1
            break
    return "\n".join(lines[begin:end])


def parse_judge_rows(text: str, test_id: str) -> list[dict]:
    """Keep ordered judge events and observed check values, including substeps."""
    rows = []
    current = None
    check = None
    category = "-"
    parents = []

    def row():
        return {"timestamp": "-", "step": "-", "description": "",
                "result": "-", "checks": []}

    def flush():
        nonlocal current, check
        if current and (current["description"] or current["checks"] or current["result"] != "-"):
            rows.append(current)
        current, check = None, None

    for line in _case_section(text, test_id).splitlines():
        if not line:
            continue
        subroutine = _SUBROUTINE.match(line)
        if subroutine:
            name, action = subroutine.groups()
            if action.lower() == "started":
                parents.append((name, current, check, category))
                current, check, category = None, None, "-"
            elif parents and parents[-1][0] == name:
                flush()
                _, current, check, category = parents.pop()
            continue
        divider = _DIVIDER.match(line)
        if divider:
            flush()
            current = row()
            # The next non-structural line is the authored step category.
            category = "-"
            continue
        if _START.search(line):
            flush()
            rows.append({**row(), "description": line})
            continue
        if _END.search(line):
            flush()
            rows.append({**row(), "step": "Conclusion", "description": line})
            continue
        current = current or row()
        if line in ("●", "▲"):
            check = {"target": "-", "expected": "-", "observed": "-",
                     "timing": "-", "passed": line == "●"}
            current["checks"].append(check)
            continue
        fields = (("Monitoring target:", "target"), ("Expected Value:", "expected"),
                  ("Observed Value:", "observed"), ("確認タイミング：", "timing"))
        field = next(((prefix, key) for prefix, key in fields if line.startswith(prefix)), None)
        if field and check is not None:
            prefix, key = field
            check[key] = line[len(prefix):].strip() or "-"
            continue
        verdict = _VERDICT.match(line)
        if verdict:
            prefix, number, result, timestamp = verdict.groups()
            current["timestamp"] = f"{timestamp}s" if timestamp else "-"
            current["step"] = category if category != "-" else f"Step{number}"
            if not current["description"]:
                current["description"] = f"{prefix + ' ' if prefix else ''}Step{number}"
            current["result"] = result.lower()
            flush()
            continue
        if category == "-" and current["description"] == "" and not re.match(r"(?:Subroutine\(|Step\d+)", line):
            category = line
            current["step"] = line
        else:
            current["description"] += ("\n" if current["description"] else "") + line
    flush()
    return rows


def _case(task) -> dict:
    attempt = task.run_count or 1
    data = None
    if task.workspace:
        root = evidence.attempt_dir(task, attempt)
        if root.parent.exists() or ".evidence" in Path(task.report_path or "").parts:
            # Integrity failures must reach the caller, never fall back to live rows.
            data = evidence.read_evidence(task, attempt, max_log_bytes=0)
    outcome = (data or {}).get("outcome") or {}
    if data is not None and not outcome and task.status in ("passed", "failed", "cancelled"):
        raise evidence.EvidenceError("Final attempt has no sealed outcome")
    source = ((data or {}).get("approved_inputs") or {}).get("row") or {}
    status = outcome.get("status", task.status)
    verdict = outcome.get("verdict", task.result or "UNKNOWN").upper()
    raw = ""
    # An unsealed attempt has no authoritative results to export yet.
    if data is None or outcome:
        path = report_service.jdgrslt_path(task, attempt if data else None)
        if path:
            raw = path.read_text(encoding="utf-8", errors="replace")
    section = _case_section(raw, task.test_id)
    if verdict in ("", "UNKNOWN") and section and status in ("passed", "failed"):
        from ..runners.test_runner import parse_verdict_text
        verdict = parse_verdict_text(section)
    if (data is not None and not outcome) or status in ("queued", "running") or (status == "cancelled" and not section):
        result = "NoNeed"
    elif verdict == "FAIL":
        result = "Failed"
    elif verdict == "PASS" and status == "passed":
        result = "Passed"
    else:
        result = "T.B.D"
    rows = parse_judge_rows(raw, task.test_id)
    return {"id": task.test_id, "name": source.get("test_name") or source.get("title") or task.task_name or task.test_id,
            "purpose": source.get("purpose") or "-", "category": source.get("category_name") or "-",
            "status": result, "rows": rows}


def _project_cases(project_id: int) -> list[dict]:
    """Read all materialized test rows, pairing each with its latest live task."""
    rows = (TestItemRow.query.filter_by(project_id=project_id, sheet="test", deleted_at=None)
            .order_by(TestItemRow.row_order.asc(), TestItemRow.id.asc()).all())
    tasks = {task.test_id: task for task in task_service.latest_per_test(project_id)}
    executed = {}
    cases = []
    for row in rows:
        identifier = row_test_id(row)
        task = tasks.get(identifier) if identifier else None
        if task is not None:
            if identifier not in executed:
                executed[identifier] = _case(task)
            cases.append(executed[identifier])
        else:
            cases.append({"id": identifier or "-", "name": row.get_field("test_name") or "-",
                          "purpose": row.get_field("purpose") or "-",
                          "category": row.get_field("category_name") or "-",
                          "status": "NoNeed", "rows": []})
    return cases


def build_qualification_report(project) -> tuple[io.BytesIO, str]:
    """Export every current project case without changing stored execution evidence."""
    report = {"suites": [{"name": project.name, "cases": _project_cases(project.id)}]}
    basename = "Qualification_Test_Report_" + datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    files = render_reports(report, basename)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content.encode("utf-8"))
    buffer.seek(0)
    return buffer, basename + ".zip"
