"""The workspace task list must stay feature-equivalent to the project one.

工作台 · 最近任务 and 任务/运行 show the same rows and must offer the same verbs.
The failure mode this file guards is drift: someone adds a column or an action
to the project task list and the workspace quietly keeps the old, poorer table --
which is exactly how the workspace ended up with a single 查看 link in the first
place.

The checks are source-level on purpose. The behaviour lives in browser
JavaScript, which the Python suite cannot execute, but "both pages render rows
through the one shared module" is a structural property that text can prove and
that is precisely what stops the two implementations diverging again.
"""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
TPL = ROOT / "app" / "templates" / "lanmatrix"
JS = ROOT / "app" / "static" / "js" / "lanmatrix"

HOME_HTML = (TPL / "home.html").read_text(encoding="utf-8")
TASKS_HTML = (TPL / "project_tasks.html").read_text(encoding="utf-8")
BASE_HTML = (TPL / "base_lm.html").read_text(encoding="utf-8")
HOME_JS = (JS / "home.js").read_text(encoding="utf-8")
TASKS_JS = (JS / "project_tasks.js").read_text(encoding="utf-8")
ROW_JS = (JS / "task_row.js").read_text(encoding="utf-8")
UI_JS = (JS / "ui.js").read_text(encoding="utf-8")


def _sort_keys(html: str) -> list[str]:
    return re.findall(r'data-sort="([^"]+)"', html)


def test_shared_row_module_is_loaded_for_every_page():
    """One <script> in the base layout, so no page can forget it."""
    assert "js/lanmatrix/task_row.js" in BASE_HTML
    # pill.js supplies the zh vocabulary task_row.js reads at definition time.
    assert BASE_HTML.index("js/lanmatrix/pill.js") < BASE_HTML.index("js/lanmatrix/task_row.js")


@pytest.mark.parametrize("source", [HOME_JS, TASKS_JS],
                         ids=["home.js", "project_tasks.js"])
def test_both_lists_render_through_the_shared_module(source):
    assert "LMTaskRow.rowHtml" in source or "TR.rowHtml" in source


@pytest.mark.parametrize("source", [HOME_JS, TASKS_JS],
                         ids=["home.js", "project_tasks.js"])
def test_neither_list_reimplements_the_row_markup(source):
    """The row <tr> is built in exactly one place."""
    assert 'class="lm-cell-progress"' not in source
    assert 'class="lm-task-sel"' not in source


def test_workspace_offers_the_same_columns_plus_project():
    """Same sortable columns as the project list, plus 项目 for the extra cell."""
    project_keys = _sort_keys(TASKS_HTML)
    home_keys = _sort_keys(HOME_HTML)
    assert set(project_keys) <= set(home_keys)
    assert "project" in home_keys


def test_workspace_offers_the_same_batch_actions():
    for verb in ("download", "cancel", "retest", "delete"):
        assert f'id="lm-h-batch-{verb}"' in HOME_HTML
        assert f'id="lm-batch-{verb}"' in TASKS_HTML


def test_workspace_has_select_all():
    assert 'id="lm-h-check-all"' in HOME_HTML


def test_workspace_batches_fan_out_per_project():
    """Every task endpoint is /projects/<pid>/..., and the workspace selection
    spans projects, so a batch that assumed one project id would act on the
    wrong rows."""
    assert "groupByProject" in HOME_JS
    assert "for (const [projectId, keys] of groupByProject(" in HOME_JS


def test_workspace_delete_button_is_per_project():
    """task.delete is project_admin only, and the workspace mixes projects the
    user administers with projects they merely read."""
    assert "canDeleteIn" in HOME_JS
    assert "p.can_delete" in HOME_JS


def test_workspace_opens_the_steps_dialog():
    assert "_steps_dialog.html" in HOME_HTML
    assert "js/lanmatrix/steps_editor.js" in HOME_HTML
    assert "LMStepsEditor.open" in HOME_JS


def test_workspace_detail_stays_a_deep_link():
    """The live-log/judge panel lives on the project page; the workspace links
    into it (with ?from=workspace so the user can get back) instead of shipping
    a second copy that would drift."""
    assert "from=workspace" in HOME_JS


def test_toast_is_shared_not_page_local():
    """workspace_reviews.js used to call a no-op toast(), so 通过/驳回 gave no
    feedback at all."""
    assert "toast: function" in UI_JS
    assert 'getElementById("lm-toast")' in UI_JS


def test_shared_module_marks_rows_with_their_project():
    """Row buttons carry data-p so a cross-project list can route each click to
    the right project's endpoint."""
    assert 'data-p="' in ROW_JS
