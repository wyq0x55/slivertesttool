"""Keep the report's positioning tables out of the accessibility tree.

The generated reports reproduce a vendor's printed layout, so the same HTML is
shown in a browser, converted to Word/PDF and read by assistive technology. A
browser only paints the positioning tables (header band, logo + heading,
single-cell wrappers, label/value grids). A screen reader, however, announces
every ``<table>`` as "table with N rows and M columns", so the layout
scaffolding was read out as if it were data (WCAG 1.3.1 Info and
Relationships; modern-web-guidance ``accessibility``: "Don't use tables for
layout").

The layout tables are therefore marked ``role="presentation"``, which drops
the table semantics while keeping their cells' content in the reading order.
Tables that carry ``<th>`` cells convey real structure and must keep it --
``role="presentation"`` there would erase the column relationships
(modern-web-guidance ``responsive-table``).

These tests pin both halves of that rule on a rendered report, and pin the
XHTML well-formedness of the output at the same time.
"""

import re
import xml.etree.ElementTree as ET

from app.services.qualification_report_renderer import render_reports
from test_qualification_report import case, report

TABLE_TAG = re.compile(r"<(/?)table\b([^>]*)>", re.IGNORECASE)
HEADER_CELL = re.compile(r"<th\b", re.IGNORECASE)
ROLE = re.compile(r'role\s*=\s*"([^"]*)"')
CLASS = re.compile(r'class="([^"]*)"')
# The grids that carry data: the case list and the statistics rows. They stay
# tables even without <th> cells, because they convey a row/column relation.
DATA_CLASSES = {"OverviewTable", "ResultTable"}
LOGO = re.compile(r'<img\b[^>]*\bsrc="data:[^"]*"[^>]*>', re.IGNORECASE)
ALT = re.compile(r'\balt="([^"]*)"')

# Two HTML-only constructs in the rendered page are pre-existing and outside
# this change: the inline <script> keeps raw JavaScript ("i < cases.length")
# and a handful of void elements are written without the XHTML " />". Both are
# normalised out before the strict XML parse; everything else must be well
# formed.
SCRIPT_BLOCK = re.compile(r"<script\b[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
VOID_ELEMENT = re.compile(
    r"<(br|img|meta|hr|input|link|area|base|col|embed|param|source|track|wbr)\b([^<>]*?)\s*/?>",
    re.IGNORECASE,
)


def _pages():
    """Render one report, mixing statuses so both views carry real content."""
    return render_reports(
        report([case("Passed"), case("Failed"), case("Untested")]), "Report"
    )


def _tables(page):
    """Yield ``(opening tag attributes, inner HTML)`` for every table."""
    open_tables = []
    for match in TABLE_TAG.finditer(page):
        if match.group(1):
            assert open_tables, f"unbalanced </table> in rendered report: {match.group(0)}"
            start, attrs = open_tables.pop()
            yield attrs, page[start : match.start()]
        else:
            open_tables.append((match.end(), match.group(2)))
    assert not open_tables, "unbalanced <table> in rendered report"


def _role(attrs):
    match = ROLE.search(attrs)
    return match.group(1) if match else None


def test_positioning_tables_are_marked_presentational():
    """The layout scaffolding is marked; the data grids keep their semantics."""
    for name, page in _pages().items():
        for attrs, _inner in _tables(page):
            cls = CLASS.search(attrs)
            cls = cls.group(1) if cls else None
            if cls in DATA_CLASSES:
                assert _role(attrs) is None, (
                    f"{name}: <table{attrs}> conveys data and must keep its "
                    "table semantics"
                )
            else:
                assert _role(attrs) == "presentation", (
                    f"{name}: <table{attrs}> positions content, so a screen reader "
                    "must not announce it as a data table"
                )


def test_presentational_tables_never_hold_header_cells():
    """A presentational table must not erase a data table's structure."""
    for name, page in _pages().items():
        for attrs, inner in _tables(page):
            if _role(attrs) == "presentation":
                assert not HEADER_CELL.search(inner), (
                    f"{name}: <table{attrs}> is presentational but contains a <th> "
                    "cell, so its structure would be lost to assistive technology"
                )


def test_tables_with_headers_keep_their_semantics():
    """The data tables (ResultTable) carry no role attribute at all."""
    seen = 0
    for name, page in _pages().items():
        for attrs, inner in _tables(page):
            if not HEADER_CELL.search(inner):
                continue
            seen += 1
            assert not ROLE.search(attrs), (
                f"{name}: <table{attrs}> holds header cells and must keep its "
                "table semantics"
            )
    assert seen, "expected the report to render its data tables with <th> cells"


def test_header_logo_carries_an_alt_attribute():
    """The inline logo image is the only thing naming the vendor."""
    for name, page in _pages().items():
        logo = LOGO.search(page)
        assert logo, f"{name}: expected the inline logo image in the header"
        alt = ALT.search(logo.group(0))
        assert alt is not None, f"{name}: the header logo has no alt attribute"
        assert alt.group(1).strip(), (
            f"{name}: the logo alt is empty, but no other text in the report "
            "names the vendor it shows"
        )


def test_rendered_reports_still_parse_as_xml():
    """The reports are XHTML documents; rendering must stay well formed.

    The cases carry no result rows: a row adds the pre-existing ``&emsp;``
    and ``<br>`` idioms (suite.html), which are outside this change, so this
    parses the header, summary and per-case structure.
    """
    for name, page in _pages().items():
        normalised = VOID_ELEMENT.sub(
            lambda m: f"<{m.group(1).lower()}{m.group(2)} />", SCRIPT_BLOCK.sub("", page)
        )
        try:
            ET.fromstring(normalised)
        except ET.ParseError as exc:  # pragma: no cover - failure path
            raise AssertionError(f"{name}: rendered report is not well-formed XML: {exc}")