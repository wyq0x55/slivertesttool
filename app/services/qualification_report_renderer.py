"""Render self-contained Silver qualification reports for offline review."""

from pathlib import Path
import re

from jinja2 import Environment, FileSystemLoader, select_autoescape


_TEMPLATE_DIRECTORY = Path(__file__).resolve().parents[1] / "templates" / "reports"
_ENVIRONMENT = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIRECTORY)),
    autoescape=select_autoescape(("html",)),
    keep_trailing_newline=True,
)
_STATUSES = {
    "Passed": ("PositiveResultCell", "TestcaseHeadingPositiveResult"),
    "Failed": ("NegativeResultCell", "TestcaseHeadingNegativeResult"),
    "T.B.D": ("TBDResultCell", "TestcaseHeadingTBDResult"),
    "Untested": ("NoneedResultCell", "TestcaseHeadingDefaultResult"),
}
_GENERAL_FIELDS = (
    "vehicle", "document", "owner", "project_owner", "software_version",
    "test_db_version", "date", "conclusion", "remarks",
)


def _text(value):
    return "-" if value is None or value == "" else str(value)


def _percentage(numerator, denominator):
    # The reference uses whole percentages, rounding halves upward.
    return (200 * numerator + denominator) // (2 * denominator) if denominator else 0


def _statistics(cases):
    total = len(cases)
    passed = sum(case["status"] == "Passed" for case in cases)
    failed = sum(case["status"] == "Failed" for case in cases)
    tbd = sum(case["status"] == "T.B.D" for case in cases)
    executed = passed + failed + tbd
    untested = total - executed
    return {
        "total": total, "passed": passed, "failed": failed, "tbd": tbd,
        "executed": executed, "untested": untested,
        "executed_percent": _percentage(executed, total),
        "untested_percent": _percentage(untested, total),
        "passed_percent": _percentage(passed, executed),
        "failed_percent": _percentage(failed, executed),
        "tbd_percent": _percentage(tbd, executed),
    }


def _banner(stats):
    if stats["failed"]:
        return "NegativeResult", "Test failed"
    if stats["tbd"]:
        return "TBDResultCell", "Test T.B.D"
    if not stats["executed"]:
        return "NoneResult", "Test untested"
    if stats["untested"]:
        return "InconclusiveResult", "Test incomplete"
    return "PositiveResult", "Test passed"


def _case_view(case, index):
    status = case.get("status", "Untested")
    if status not in _STATUSES:
        status = "Untested"
    result_class, heading_class = _STATUSES[status]
    rows = []
    for row in case.get("rows", []):
        result = row.get("result", "-")
        if result not in ("passed", "failed"):
            result = "-"
        rows.append({
            **{key: _text(row.get(key)) for key in ("timestamp", "step", "description")},
            "result": result,
            "result_class": {
                "passed": "PositiveResultCell", "failed": "NegativeResultCell",
            }.get(result, "DefaultCell"),
            "checks": [
                {
                    **{key: _text(check.get(key)) for key in ("target", "expected", "observed", "timing")},
                    "class": "judgeOK" if check.get("passed") is True else "judgeNG",
                }
                for check in row.get("checks", [])
            ],
        })
    return {
        **{key: _text(case.get(key)) for key in ("id", "name", "purpose", "category")},
        "anchor": f"case-{index}", "status": status,
        "result_class": result_class, "heading_class": heading_class, "rows": rows,
    }


def render_reports(report: dict, basename: str) -> dict[str, str]:
    """Return an overview and linked suite pages without filesystem writes.

    Filenames and anchors are generated independently of untrusted report text.
    Every dynamic field is escaped by Jinja, including descriptions and checks.
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", basename):
        raise ValueError("Report basename must contain only letters, digits, underscores and hyphens")
    general = {key: _text(report.get("general", {}).get(key)) for key in _GENERAL_FIELDS}
    overview_filename = f"{basename}.html"
    suites = []
    used_filenames = {overview_filename}
    for index, suite in enumerate(report.get("suites", []), start=1):
        name = _text(suite.get("name"))
        suffix = name if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name) else f"suite_{index}"
        filename = f"{basename}_{suffix}.html"
        counter = 2
        while filename in used_filenames:
            filename = f"{basename}_{suffix}_{counter}.html"
            counter += 1
        used_filenames.add(filename)
        cases = [_case_view(case, number) for number, case in enumerate(suite.get("cases", []), start=1)]
        suites.append({"name": name, "filename": filename, "cases": cases, "stats": _statistics(cases)})

    stats = _statistics([case for suite in suites for case in suite["cases"]])
    banner_class, banner_text = _banner(stats)
    common = {"general": general, "title": basename, "overview_filename": overview_filename}
    pages = {
        overview_filename: _ENVIRONMENT.get_template("overview.html").render(
            **common, suites=suites, stats=stats, banner_class=banner_class, banner_text=banner_text,
        ),
    }
    for suite in suites:
        banner_class, banner_text = _banner(suite["stats"])
        suite_common = {**common, "general": {key: "親レポート参照" for key in _GENERAL_FIELDS}}
        pages[suite["filename"]] = _ENVIRONMENT.get_template("suite.html").render(
            **suite_common, suite=suite, stats=suite["stats"], banner_class=banner_class, banner_text=banner_text,
        )
    return pages
