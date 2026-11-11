"""Structuring a jdgrslt case section into per-step results and checks."""

from app.runners.test_runner import parse_step_results

LOG = """INFO:root:Test case ID.ID;;TC-1 is started!
INFO:root:-------------------pre_init-------------------
INFO:root:前提条件の確認
INFO:root: Subroutine(SystemInitialize) is started!
INFO:root:------------------- Subroutine(SystemInitialize) Step1-------------------
INFO:root: Subroutine(SystemInitialize) Step1 初期化する。
INFO:root:Step.1 is passed at 0.0s.
INFO:root:-------------------Step2-------------------
INFO:root:結果確認
INFO:root:●
INFO:root:    Monitoring target: State ( u1_state )
INFO:root:    Expected Value:Ready ( (exp==READY(1)) )
INFO:root:    Observed Value:0x1 ( 1.0 )
INFO:root:    確認タイミング：任意
INFO:root:Step.2 is passed at 0.006s.
INFO:root:-------------------Step3-------------------
INFO:root:トリガ入力
INFO:root: Subroutine(SetTrigger) is started!
INFO:root:------------------- Subroutine(SetTrigger) Step1-------------------
INFO:root:●
INFO:root:    Monitoring target: Mode ( u1_mode )
INFO:root:    Expected Value:OFF ( (exp==OFF(0)) )
INFO:root:    Observed Value:0x0 ( 0.0 )
INFO:root:    確認タイミング：任意
INFO:root:SetTrigger Step.1 is passed at 0.006s.
INFO:root:▲
INFO:root:    Monitoring target: Output ( u1_out )
INFO:root:    Expected Value:ON ( (exp==ON(1)) )
INFO:root:    Observed Value:0x0 ( 0.0 )
INFO:root:    確認タイミング：2.0s以内
INFO:root:Step.3 is failed at 2.001s.
INFO:root:Test is failed in Step3!!!!
"""


def test_steps_carry_results_descriptions_and_checks():
    steps = parse_step_results(LOG)
    assert [(s["step"], s["result"], s["at"]) for s in steps] == [
        (1, "PASS", "0.0s"), (2, "PASS", "0.006s"), (3, "FAIL", "2.001s")]
    assert steps[0]["phase"] == "pre_init" and steps[0]["description"] == ["前提条件の確認"]
    assert steps[1]["checks"] == [{"ok": True, "target": "State ( u1_state )", "expected": "Ready ( (exp==READY(1)) )",
                                   "observed": "0x1 ( 1.0 )", "timing": "任意"}]
    sub, failed = steps[2]["checks"]
    assert (sub["subroutine"], sub["subroutine_step"], sub["ok"]) == ("SetTrigger", 1, True)
    assert failed == {"ok": False, "target": "Output ( u1_out )", "expected": "ON ( (exp==ON(1)) )",
                      "observed": "0x0 ( 0.0 )", "timing": "2.0s以内"}
    assert steps[2]["description"] == ["トリガ入力"]


def test_empty_section_has_no_steps():
    assert parse_step_results("") == []
