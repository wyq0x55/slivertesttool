"""Heading invariants for the lanmatrix page templates.

Why: a screen-reader user navigates a page by jumping between headings (the
"heading level 1" shortcut), and that navigation needs an entry point — one
top-level <h1> per page (WCAG 1.3.1 Info and Relationships, Level A;
2.4.6 Headings and Labels, Level AA). A breadcrumb <b> or the <title> is not
a heading, so a page whose only title lives there is unreachable by heading
navigation.

The matrix editor is a full-bleed grid, so its <h1> is visually hidden with
the .lm-sr-only utility rather than removed: it must stay present in the
accessibility tree for the page to keep its entry point.

The checks read the templates as text, so they need no database, no Flask
app and no browser.
"""

import re

from pathlib import Path

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "app" / "templates" / "lanmatrix"
APP_CSS = Path(__file__).resolve().parents[1] / "app" / "static" / "css" / "app.css"

JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)
EXTENDS_BASE = re.compile(r'\{%-?\s*extends\s+"lanmatrix/base_lm.html"\s*-?%\}')
CONTENT_BLOCK = re.compile(r"\{%-?\s*block\s+content\s*-?%\}")
H1 = re.compile(r"<h1\b([^>]*)>(.*?)</h1>", re.DOTALL | re.IGNORECASE)
CLASS_ATTRIBUTE = re.compile(r'\bclass\s*=\s*"([^"]*)"', re.IGNORECASE)


def strip_jinja_comments(source):
    return JINJA_COMMENT.sub("", source)


def read_template(name):
    """Return the template source with Jinja comments removed."""
    return strip_jinja_comments((TEMPLATE_DIR / name).read_text(encoding="utf-8"))


def base_page_sources():
    """Map filename -> source for every page that extends base_lm.html.

    Partials (_*.html) and base_lm.html itself do not extend the base, so the
    sweep covers exactly the pages that render as a full document.
    """
    pages = {}
    for path in sorted(TEMPLATE_DIR.glob("*.html")):
        source = strip_jinja_comments(path.read_text(encoding="utf-8"))
        if EXTENDS_BASE.search(source):
            pages[path.name] = source
    return pages


def pages_without_h1(pages):
    """Return the sorted names of pages that declare no <h1>."""
    return sorted(name for name, source in pages.items() if not H1.search(source))


def test_the_sweep_covers_the_editor_page():
    pages = base_page_sources()
    assert "editor.html" in pages, (
        "the base_lm.html sweep found no editor.html; the {% extends %} "
        f"pattern or the template directory has changed: {sorted(pages)}"
    )
    assert len(pages) > 1, f"the sweep found only one page: {sorted(pages)}"


def test_every_base_page_declares_a_top_level_heading():
    missing = pages_without_h1(base_page_sources())
    assert not missing, (
        "pages with no <h1>, so heading-level-1 navigation has no entry "
        f"point: {missing}"
    )


def test_the_check_flags_a_page_with_no_h1():
    # Self-check: the sweep above must be able to fail. A page without an h1
    # is reported, and an h1 hidden in a Jinja comment does not count.
    fixture = {
        "with_heading.html": strip_jinja_comments("<h1>页面</h1>"),
        "without_heading.html": strip_jinja_comments('<div class="page">正文</div>'),
        "commented_heading.html": strip_jinja_comments("{# <h1>旧标题</h1> #}<div>正文</div>"),
    }
    assert pages_without_h1(fixture) == ["commented_heading.html", "without_heading.html"]


def test_editor_heading_is_the_matrix_title():
    headings = H1.findall(read_template("editor.html"))
    assert len(headings) == 1, (
        f"editor.html should declare exactly one top-level heading, found {len(headings)}"
    )
    attributes, inner = headings[0]
    assert inner.strip() == "矩阵编辑", f"editor.html heading reads {inner.strip()!r}"

    classes = CLASS_ATTRIBUTE.search(attributes)
    assert classes, "the editor heading carries no class attribute"
    assert "lm-sr-only" in classes.group(1).split(), (
        "the editor heading must be visually hidden with .lm-sr-only so the "
        f"full-bleed grid stays unbroken; classes: {classes.group(1)!r}"
    )


def test_editor_heading_opens_the_content_block():
    source = read_template("editor.html")
    block = CONTENT_BLOCK.search(source)
    assert block, "editor.html has no {% block content %}"
    assert re.match(r"\s*<h1\b", source[block.end() :]), (
        "the <h1> must be the first element inside {% block content %}, "
        "so heading navigation lands at the top of the page"
    )


def test_the_hidden_heading_utility_is_defined():
    css = APP_CSS.read_text(encoding="utf-8")
    assert ".lm-sr-only" in css, (
        "app.css no longer defines .lm-sr-only; the visually hidden editor "
        "heading would render and break the full-bleed grid"
    )