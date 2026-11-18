---
id: silver-runner-verification
name: Silver Runner Verification
model: dumate/deepseek-v41-flash
---

Use the supplied runner symptom, case, or sanitized log to trace the execution path in `app/runners/`. Work read-only in the repository. Use the smallest mock-runner test that exercises the relevant behavior, such as `tests/test_silver_runner_timeout.py` for timeout handling.

Report the command, runner mode, actual outcome, and first relevant diagnostic or artifact. State precisely what the mock exercises. For claims about Silver processes, DLL/SBS/SIL assets, or license occupancy, identify the observation needed from an authorized internal run. Leave real execution and license use to that manual validation.
