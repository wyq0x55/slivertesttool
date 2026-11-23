"""Accessibility invariants for the lanmatrix page templates.

Two WCAG 1.3.1 concerns, both purely textual in the Jinja templates:

* every <table> needs an accessible name (a <caption>, aria-label or
  aria-labelledby) so assistive technology can announce what the table is;
* no <th> may be empty, because an empty header cell is announced as an
  unnamed column during table navigation.

The checks read the templates as text, so they need no database, no Flask
app and no browser.
"""

import re

from pathlib import Path

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "app" / "templates" / "lanmatrix"

TEMPLATES = [
    "admin.html",
    "admin_db.html",
    "ai_drafts.html",
    "fields.html",
    "home.html",
    "project_tasks.html",
    "ut_pipeline.html",
    "ut_tasks.html",
]

JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)
TABLE_TAG = re.compile(r"</?table\b[^>]*>", re.IGNORECASE)
TH_CELL = re.compile(r"<th\b[^>]*>(.*?)</th>", re.DOTALL | re.IGNORECASE)
CAPTION = re.compile(r"<caption\b", re.IGNORECASE)
NAME_ATTRIBUTE = re.compile(r"\b(?:aria-label|aria-labelledby)\s*=", re.IGNORECASE)


def read_template(name):
    """Return the template source with Jinja comments removed."""
    source = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
    return JINJA_COMMENT.sub("", source)


def line_number(source, offset):
    return source.count("\n", 0, offset) + 1


def tables(source):
    """Yield (opening_tag, inner_html, offset) for each table, open to close."""
    stack = []
    for tag in TABLE_TAG.finditer(source):
        if tag.group(0).lower().startswith("</table"):
            opening = stack.pop()
            yield opening.group(0), source[opening.end() : tag.start()], opening.start()
        else:
            stack.append(tag)
    assert not stack, "template has an unclosed <table>"


def test_no_header_cell_is_empty():
    empty_cells = []
    for name in TEMPLATES:
        source = read_template(name)
        for cell in TH_CELL.finditer(source):
            if not cell.group(1).strip():
                empty_cells.append(f"{name}:{line_number(source, cell.start())}")
    assert not empty_cells, f"empty <th> cells (announced as unnamed columns): {empty_cells}"


def test_every_table_has_an_accessible_name():
    unnamed = []
    for name in TEMPLATES:
        source = read_template(name)
        for opening, inner, offset in tables(source):
            if CAPTION.search(inner) or NAME_ATTRIBUTE.search(opening):
                continue
            unnamed.append(f"{name}:{line_number(source, offset)}: {opening}")
    assert not unnamed, f"tables without <caption>/aria-label/aria-labelledby: {unnamed}"