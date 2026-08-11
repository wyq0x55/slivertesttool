"""Regression cover for the flaky verdict caused by Silver's double module load.

Silver loads the judge module twice per run: once when ``add_module`` injects it
and again after ``silver.restart()``. The first instance never executes a step
and is discarded, but its teardown used to emit a full failure block into
jdgrslt.log without a ``Test case ... is started!`` marker of its own. Whether
that block landed before or after the real case's verdict was pure timing, so a
passing test intermittently reported FAIL -- under a batch, not on a lone re-run.

Three independent defences are covered here:
  A. the discarded instance emits nothing (``run_cleanup`` bails out early);
  B. the judge binds its own log file handler instead of ``basicConfig``;
  C. a case's section stops at its own verdict line, so trailing orphan output
     can no longer be attributed to it.
"""

from __future__ import annotations

import logging

import pytest

from app.runners.test_runner import (
    count_failed_steps,
    extract_case_section,
    parse_verdict_text,
)

TID = "MWCPD-ISZ-SWE.6-TC_APL-001005"

_PASSING_CASE = f"""INFO:root:2026-08-11 15:28:39.554821
INFO:root:Test case ID.ID;;{TID} is started!
INFO:root:-------------------Step1-------------------
INFO:root:Step.1 is passed at 0.0s.
INFO:root:-------------------Step2-------------------
INFO:root:Step.2 is passed at 0.026s.
INFO:root:All steps are verified.Test is Passed.
"""

# What the discarded first instance used to append -- note the absence of any
# "Test case ... is started!" line.
_ORPHAN_TEARDOWN = """INFO:root:The test was suspended !!!
INFO:root:Step.1 is failed at 0.0s.
INFO:root:Test is failed in Step1!!!!
"""


class TestVerdictSectioning:
    """Defence C: the parser must not inherit unmarked trailing output."""

    def test_teardown_after_a_pass_does_not_flip_the_verdict(self):
        text = _PASSING_CASE + _ORPHAN_TEARDOWN
        assert parse_verdict_text(text, TID) == "PASS"

    def test_teardown_after_a_pass_is_not_counted_as_a_failed_step(self):
        text = _PASSING_CASE + _ORPHAN_TEARDOWN
        assert count_failed_steps(text, TID) == 0

    def test_section_stops_at_the_verdict_line(self):
        section = extract_case_section(_PASSING_CASE + _ORPHAN_TEARDOWN, TID)
        assert "Test is Passed" in section
        assert "suspended" not in section

    def test_teardown_before_the_real_case_was_already_harmless(self):
        """The ordering that happened to work before must keep working."""
        text = (f"INFO:root:Test case ID.ID;;{TID} is started!\n"
                + _ORPHAN_TEARDOWN + _PASSING_CASE)
        assert parse_verdict_text(text, TID) == "PASS"

    def test_a_genuine_failure_is_still_reported(self):
        text = f"""INFO:root:Test case ID.ID;;{TID} is started!
INFO:root:-------------------Step1-------------------
INFO:root:Step.1 is passed at 0.0s.
INFO:root:-------------------Step2-------------------
INFO:root:Step.2 is failed at 1.5s.
INFO:root:Test is failed in Step2!!!!
"""
        assert parse_verdict_text(text, TID) == "FAIL"
        assert count_failed_steps(text, TID) == 1

    def test_a_genuine_failure_survives_trailing_noise(self):
        text = f"""INFO:root:Test case ID.ID;;{TID} is started!
INFO:root:Step.1 is failed at 1.5s.
INFO:root:Test is failed in Step1!!!!
INFO:root:-------------------pre_init-------------------
"""
        assert parse_verdict_text(text, TID) == "FAIL"

    def test_a_rerun_still_wins_over_the_previous_run(self):
        """The newest block is authoritative; sectioning must not break that."""
        failed = f"""INFO:root:Test case ID.ID;;{TID} is started!
INFO:root:Step.1 is failed at 1.5s.
INFO:root:Test is failed in Step1!!!!
"""
        assert parse_verdict_text(failed + _PASSING_CASE, TID) == "PASS"
        assert parse_verdict_text(_PASSING_CASE + failed, TID) == "FAIL"

    def test_an_unterminated_case_still_falls_back_to_step_markers(self):
        text = f"""INFO:root:Test case ID.ID;;{TID} is started!
INFO:root:Step.1 is passed at 0.0s.
"""
        assert parse_verdict_text(text, TID) == "PASS"

    def test_another_case_is_never_borrowed_from(self):
        other = """INFO:root:Test case ID.ID;;SOME-OTHER-CASE is started!
INFO:root:Step.1 is failed at 0.2s.
INFO:root:Test is failed in Step1!!!!
"""
        assert parse_verdict_text(_PASSING_CASE + other, TID) == "PASS"

    def test_empty_and_markerless_text_are_unchanged(self):
        assert parse_verdict_text("", TID) == "UNKNOWN"
        assert parse_verdict_text("INFO:root:Step.1 is passed at 0.0s.", TID) \
            == "PASS"


class TestDiscardedInstanceStaysSilent:
    """Defence A: an instance that never ran a step reports nothing."""

    @staticmethod
    def _framework():
        return pytest.importorskip(
            "app.runners.silver_json.silver_test_framework")

    def _ctx(self, framework, **state):
        ctx = framework.TestContext.__new__(framework.TestContext)
        ctx._log = logging.getLogger("test.judge.silent")
        ctx._DLL_OK = 0
        ctx.test_python_over = -1
        ctx.test_step_no = -1
        ctx.current_step = None
        for key, value in state.items():
            setattr(ctx, key, value)
        return ctx

    def test_no_output_when_no_step_was_ever_entered(self, caplog):
        framework = self._framework()
        ctx = self._ctx(framework)
        with caplog.at_level(logging.INFO):
            assert framework.run_cleanup(ctx, 0.0) == ctx._DLL_OK
        assert caplog.text == ""

    def test_no_output_when_the_test_completed_normally(self, caplog):
        framework = self._framework()
        ctx = self._ctx(framework, test_python_over=0)
        with caplog.at_level(logging.INFO):
            framework.run_cleanup(ctx, 0.0)
        assert caplog.text == ""

    def test_a_real_suspension_is_still_reported(self, caplog):
        framework = self._framework()
        step = framework.Step.__new__(framework.Step)
        step.no = 3
        step.checks = []          # ``_emit_step_detail`` iterates over these
        step.inputs = []
        step.category = ""
        step.comment = ""
        ctx = self._ctx(framework, test_step_no=3, current_step=step)
        ctx.var = lambda name: None
        with caplog.at_level(logging.INFO):
            framework.run_cleanup(ctx, 5.0)
        assert "The test was suspended !!!" in caplog.text
        assert "Test is failed in Step3!!!!" in caplog.text
