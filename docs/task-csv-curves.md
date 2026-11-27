# Variable curves (task output.csv)

Open a Silver project's **Tasks / Runs**, open a task and select **变量曲线**.
The view shows one attempt's `output.csv` as logic-analyzer lanes on a shared
time axis, with that attempt's judge verdict laid over it.

## Input contract (verified)

`app/runners/silver_runner.py` injects `CsvWriter.dll ${OUTPUTCSV} -w 0 -I 0.001 -m t`
(plus `-l output.txt` when the case ships a signal list). Real outputs from the
RWS project (2026-11-15) confirm the format the viewer relies on:

- ASCII/UTF-8 text, comma separated, a quoted header, one record per line;
- the first column is `time`, in seconds, one record per millisecond;
- every other column is one variable path (e.g. `Rte_APL_INPUT_PP_JGD_DOORSW_u1_JGD_DOORSW`,
  `SILS_CanIf_Receive_0x400h.data_signal.C_DR_DOOR`, `stl_Timer_MsTimerResource[2].u2_msec`).

A file that does not match (first column not `time`, wrong field count,
decreasing or non-numeric time) is rejected with `CSV_FORMAT`; nothing is guessed.
Non-numeric cells inside a signal are gaps, never zero.

Most columns are state signals that change a handful of times per case and about
half never change, so the server parses the file once into a **change-point
index** per column (record of each value change plus the new value) and caches
it on disk under `CURVE_CACHE_DIR`, keyed by task, attempt and file identity.
Source files are never modified.

## What the server derives

| Item | Source |
| --- | --- |
| Signal kind | `constant` (no change), `discrete` (integers, at most 32 values; 0/1 and multi-value states are both drawn as a thin step line), `analog` |
| Judge timeline | the attempt's `jdgrslt.log`: steps with start/end and result; every check with variable, expectation, observation, timing and the time of the verdict line that closed it (`parse_step_results`) |
| Display name and role | a sealed attempt's pinned procedure (`approved_inputs`: input / expected signals of the test and the subroutines it calls), plus the judge's `Monitoring target` names |
| Value labels | per variable, the constants the case writes or expects (pinned `constants.json`), plus the macros in the judge's expected values; one value can carry several names |

Legacy (unsealed) tasks have no pinned procedure, so their names and labels come
from the judge log only; the status line says so.

## Exactness and limits

Series are exact change points. Only when a signal has more change points in the
requested range than **精确点上限** is it sent as a per-pixel min/max envelope
(the tooltip says "像素内范围"); zooming in re-requests the range. Cursor values
(`/values`), edge jumps (`/edge`) and the event list are always answered from the
exact index. The raw table and the CSV exports read the original file text.

Limits are live settings in **系统管理 → 授权 / 并发** (no restart):
最大 output.csv (MiB), 精确点上限, 最大泳道数, 索引缓存上限 (MiB).

## Using the view

- Left: variables grouped by name prefix. The checkbox on a category selects or
  clears every variable in that group (up to the lane limit; changing signals
  before constants). Filter 判定涉及 / 有变化 / 全部; constant variables are folded
  under 恒定 with their value. 批量选变量 opens a dialog to paste variable names
  or `*` wildcard patterns (one per line, `#` comments ignored) to replace or
  append lanes, or fill the current lanes into the box. The default view lists
  expected/check variables first, then stimulus inputs.
- Lanes: drag a label to reorder, drag its bottom edge to resize, × to remove.
  Top band: judge steps (failed in red); click a step to zoom to it, double-click
  to open it in 判定结果. Check markers sit on their lane at the verdict time
  (◆ passed, ▲ failed); hover for expected/observed.
- Wheel zooms, drag pans, dragging on the top time axis zooms to the selected
  span, and the minimap selects the window. Click sets cursor A, right-click or
  Shift+click sets B (Δt shown), and either cursor line can be dragged directly.
  放大 A–B zooms to the span between A and B, 光标测量 lists each lane's exact A
  and B values with signed `右 − 左` delta (clicking a row highlights that lane),
  and × next to A or B clears that cursor. Select a lane and use ←/→ (Shift for B)
  to jump to its previous/next change. `0` shows everything, `F` jumps to the
  first failure.
- 判定结果 → 曲线 on any step opens the curves focused on that step.
- 事件列表: every change in time order (lanes / current range filters); 判定检查点:
  all checks; 原始数据: original records.
- 对比: another attempt of the task or the latest attempt of another task in the
  project. Their values are drawn dashed in the same lanes, and the first time each
  common variable differs is marked (the overall first divergence is listed and
  its variable added as a lane).
- 视图: save lanes (by variable name), heights and time range per test case;
  shared with project members, editable by its author or a project admin.
  复制链接 copies a URL that restores attempt, lanes, range, cursors and comparison.
- 导出: the current view as PNG or SVG (same renderer), the selected variables in
  the current range as CSV (original text), or the original `output.csv`.
- A running attempt is shown live: it reads the output.csv Silver is writing in
  the pool run directory (a trailing partial record is ignored) and refreshes every
  2 s. Whether CsvWriter flushes during a run (`-w 0`) still needs to be confirmed
  on a real Silver run; if it only writes at the end, the view says that
  output.csv has not been written yet until the run finishes.

The project HTML qualification report embeds, for each failed case, an SVG of the
first failed step's window (the checked variables and stimulus inputs).

## API

All paths are under `/api/v1/projects/<project_id>/tasks/<task_key>/curves`.
`run` selects an attempt (default: current); the current attempt of a running task
is live. Non-live reads may pass `revision` and fail with 409 if it changed.

| Path | Permission | Result |
| --- | --- | --- |
| base | task.view | index metadata, attempts, judge timeline, signal dictionary |
| `/series?columns=c1,c2&start&end&width` | task.view | change points or envelope per column |
| `/values?columns&at` | task.view | exact values at a time |
| `/edge?column&at&dir=1\|-1` | task.view | next / previous change |
| `/events?columns&start&end&offset&limit` | task.view | changes in time order |
| `/compare?other=<task_key>&other_run&columns` | task.view | first divergence per common variable |
| `/raw?offset&count` | task.download | original records |
| `/export?columns&start&end` | task.download | original text of the selection as CSV |
| `/download` | task.download | original bytes |

Saved views: `GET/POST /api/v1/projects/<id>/curve-views` (`test_id`, `name`,
`layout: {lanes: [{name, height?}], range?}`), `PUT/DELETE .../curve-views/<view_id>`.
