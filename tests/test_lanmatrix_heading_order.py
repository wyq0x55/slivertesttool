"""Heading order for the lanmatrix pages that host their own sections.

Why: a screen-reader user navigates by heading level, so the outline must not
skip a level (WCAG 1.3.1 Info and Relationships, Level A). These pages had
their page sections one or two levels below their <h1> (an <h3> or <h4> right
after it), which reads as "this section is nested under something that is not
there". A page section is now an <h2>; a genuinely nested subsection stays an
<h3>.

Modal dialogs are not part of the page outline — they are separate views with
their own heading — so their headings are excluded from the check. The task
detail in project_tasks.html is likewise a separate overlay (its own <h1>),
not a section of the list page, and is excluded with its id.

The checks read templates and CSS as text, so they need no database, no Flask
app and no browser.
"""

import re

from html.parser import HTMLParser
from pathlib import Path

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "app" / "templates" / "lanmatrix"
CSS_DIR = Path(__file__).resolve().parents[1] / "app" / "static" / "css"

PAGES = (
    "dashboard.html",
    "admin.html",
    "admin_db.html",
    "fields.html",
    "projects.html",
    "home.html",
    "project_tasks.html",
)

# Overlay that renders its own document-like view, so its headings are not
# part of the list page's outline.
SKIPPED_SECTION_IDS = {"lm-task-detail"}

HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)
WHITESPACE = re.compile(r"\s+")


class HeadingOutline(HTMLParser):
    """Collect headings in source order, skipping dialog and overlay trees."""

    def __init__(self, skipped_ids=()):
        super().__init__(convert_charrefs=True)
        self._skipped_ids = set(skipped_ids)
        self._stack = []
        self._current = None
        self._text = []
        self.headings = []

    def _excluded(self):
        for tag, attrs in self._stack:
            if tag == "dialog":
                return True
            if dict(attrs).get("id") in self._skipped_ids:
                return True
        return False

    def handle_starttag(self, tag, attrs):
        excluded = self._excluded()
        self._stack.append((tag, attrs))
        if tag in HEADING_TAGS and self._current is None and not excluded:
            self._current = (tag, dict(attrs))
            self._text = []

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self._current is not None and tag == self._current[0]:
            tag_name, attrs = self._current
            self.headings.append(
                {
                    "tag": tag_name,
                    "level": int(tag_name[1]),
                    "id": attrs.get("id"),
                    "class": attrs.get("class") or "",
                    "text": WHITESPACE.sub(" ", "".join(self._text)).strip(),
                }
            )
            self._current = None
            self._text = []
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                break

    def handle_data(self, data):
        if self._current is not None:
            self._text.append(data)


def read_template(name):
    source = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
    return JINJA_COMMENT.sub("", source)


def outline_of(source, skipped_ids=()):
    parser = HeadingOutline(skipped_ids)
    parser.feed(source)
    parser.close()
    return parser.headings


def page_outline(name):
    skipped = SKIPPED_SECTION_IDS if name == "project_tasks.html" else ()
    return outline_of(read_template(name), skipped)


def by_id(headings):
    return {heading["id"]: heading for heading in headings if heading["id"]}


def assert_no_skipped_levels(name, headings):
    assert headings, name + " has no headings"
    assert headings[0]["level"] == 1, name + " does not start at h1"
    previous = 1
    for heading in headings[1:]:
        assert heading["level"] <= previous + 1, (
            "%s skips a heading level: h%d %r follows h%d %r"
            % (
                name,
                heading["level"],
                heading["text"],
                previous,
                "section",
            )
        )
        previous = heading["level"]


def test_page_sections_do_not_skip_heading_levels():
    for name in PAGES:
        assert_no_skipped_levels(name, page_outline(name))


def test_page_sections_are_second_level():
    dashboard = by_id(page_outline("dashboard.html"))
    for heading_id in ("lm-ut-repair-title", "lm-vc-title"):
        assert dashboard[heading_id]["tag"] == "h2", heading_id

    admin = page_outline("admin.html")
    assert by_id(admin)["lm-admin-users-title"]["tag"] == "h2"
    assert by_id(admin)["lm-admin-tasks-title"]["tag"] == "h2"
    assert any(h["tag"] == "h2" and "运行时配置" in h["text"] for h in admin)

    admin_db = page_outline("admin_db.html")
    assert [h["tag"] for h in admin_db[:3]] == ["h1", "h2", "h3"]
    assert "连接概览" in admin_db[1]["text"]
    assert "SQL 控制台" in admin_db[3]["text"]

    assert by_id(page_outline("fields.html"))["lm-fields-list-title"]["tag"] == "h2"


def test_nested_subsections_stay_third_level():
    dashboard = by_id(page_outline("dashboard.html"))
    assert dashboard["lm-vc-history-title"]["tag"] == "h3"

    subhead = [h for h in page_outline("admin_db.html") if "lm-db-subhead" in h["class"]]
    assert len(subhead) == 1 and subhead[0]["tag"] == "h3"


def test_dialogs_stay_outside_the_page_outline():
    admin = read_template("admin.html")
    fields = read_template("fields.html")
    projects = read_template("projects.html")

    assert '<h3 id="lm-user-dlg-title"' in admin
    assert '<h3 id="lm-field-dialog-title"' in fields
    assert '<h3 id="lm-new-dlg-title"' in projects
    assert "lm-user-dlg-title" not in by_id(page_outline("admin.html"))
    assert "lm-field-dialog-title" not in by_id(page_outline("fields.html"))
    assert "lm-new-dlg-title" not in by_id(page_outline("projects.html"))


def test_project_detail_overlay_keeps_its_own_heading():
    source = read_template("project_tasks.html")
    assert '<section id="lm-task-detail"' in source
    assert '<h1 id="lm-d-title">' in source

    detail = outline_of(source.rsplit('<section id="lm-task-detail"', 1)[1])
    assert detail[0]["tag"] == "h1"
    # The overlay is not part of the page outline checked above.
    assert not any(h["id"] == "lm-d-title" for h in page_outline("project_tasks.html"))


def test_skipped_level_is_flagged():
    outline = outline_of("<h1>Page</h1><h3>Section</h3>")
    try:
        assert_no_skipped_levels("sample", outline)
    except AssertionError as error:
        assert "skips a heading level" in str(error)
    else:
        raise AssertionError("a skipped heading level was not detected")


def test_promoted_headings_keep_their_component_style():
    design_system = (CSS_DIR / "design-system.css").read_text(encoding="utf-8")
    review = (CSS_DIR / "lanmatrix-review.css").read_text(encoding="utf-8")
    lanmatrix = (CSS_DIR / "lanmatrix.css").read_text(encoding="utf-8")

    # Each promoted heading stays covered by the rule that used to size its
    # h3/h4 counterpart, so its computed presentation does not change.
    assert ".tablecard .tc-head h2," in design_system
    assert ".state h2," in design_system
    assert ".ctrl-card .tc-head h2{" in design_system
    assert ".lm-db-body .lm-db-subhead{" in design_system
    assert ".lm-db-body h2," in lanmatrix
    assert ".lm-chart-card h2," in review
    assert ".lm-chart-card #lm-vc-history > h3 {" in review