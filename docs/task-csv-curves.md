# Task CSV curves

Open a Silver project's **Tasks / Runs**, open a task, and select **Variable curves**.
The viewer reads that task's current-attempt `output.csv`. It uses the existing
project `task.view` permission; downloading requires `task.download`. No runner is
started. The explicitly labeled synthetic preview remains a separate opt-in mode.

## Verified contract and remaining validation

`app/runners/silver_runner.py` configures `OUTPUTCSV` as the task log directory's
`output.csv`, then injects `CsvWriter.dll ${OUTPUTCSV} -w 0 -I 0.001 -m t`, optionally
adding `-l ${OUTPUTTXT}` when the test case supplies an `output.txt` signal-selection
list. That list is an input, not generated variable metadata. The code does not
establish a CSV header, encoding, separator, unit, boolean type, or time-unit
contract. The flag names alone are not sufficient evidence to invent one.

No proprietary Silver-produced CSV sample was available for this implementation.
The checked-in `tests/fixtures/curves/` files are representative **fixtures**, not
Silver evidence. An internal real-Silver check of the actual CsvWriter header and
format remains required; neither mock CI nor the local browser demonstration proves
Silver, license, or proprietary DLL behavior.

## Supported text formats

- First record: CSV column names, preserved verbatim in raw data. Blank names get
  a display label; duplicate names retain distinct positional IDs (`c0`, `c1`, ...).
- UTF-8, UTF-8 BOM, UTF-16 BOM, GB18030 and Windows-1252. Ambiguous legacy encodings
  require an explicit choice; decoding errors are not silently replaced.
- Comma, semicolon, tab or pipe separators, quoted fields and CRLF/LF. The optional
  Excel `sep=;` directive is recognized. Separator choice can be overridden.
- Finite numeric time and signal values, including scientific notation. Decimal
  commas are accepted only with a non-comma separator. Text, blank values, NaN and
  infinity become null gaps, not zero. Invalid-time and malformed records are
  counted and excluded from curves, but raw records remain inspectable.
- A conventional numeric `Time`, `t`, `time_s`, `Time [s]`, `time[s]` or `timestamp`
  header is suggested. The user can explicitly choose any time column. Values
  are never converted to dates or seconds implicitly.
- Units and signal types remain unknown. The viewer does not parse array indices
  or arbitrary header suffixes as units and does not infer boolean signals from
  0/1 values. Units and staircase rendering can be set explicitly for display.
  Unknown units get separate tracks; equal explicitly supplied units may share
  a track, with at most three series per track and eight selected series overall.
- Duplicate times retain source-record order and are reported. The original-point
  lookup chooses the first record at the nearest time and reports duplicate count.
  Decreasing time is rejected rather than silently sorted or joined misleadingly.

## Bounds and sampling

Preview and direct CSV download are limited to 64 MiB per file. Parsing is streamed
with an enforced read budget, at most one million data records, 256 columns, 128 Ki
characters per physical line, and the standard CSV field-size bound. Files exceeding
the preview limit can still be retrieved through the existing run report download.
Metadata inspects at most 100 records. Raw pages contain at most 100 records, with
32 Ki characters per raw record. Requests above limits fail explicitly.

A curve response contains at most 2,000 shared time records for up to eight signals.
Within each time bucket the sampler retains first/last records, per-signal minimum
and maximum records, and a null-gap record. This preserves short extrema better
than uniform stride sampling, but does not guarantee every rapid transition or gap
survives. Source record numbers are carried with all plotted values. Duplicate
records are not merged. The view states whether sampling occurred. Zoom selects a
shared range; **refine selected range** re-reads and resamples that range server-side.
Raw cursor lookup scans original records and returns exact original CSV text; the
raw table and CSV download do not use sampled values.

All reads, including cursor lookups, remain bounded. Sealed attempts validate the
committed manifest/outcome identity and the selected CSV digest without scanning
unrelated model/input artifacts. Missing or corrupt sealed artifacts cannot fall
back to mutable legacy logs. Legacy tasks use only their established
`workspace/log/<test_id>/output.csv` location, never an arbitrary stored report path.
Symlinks/reparse points and unsafe task/file identities are rejected. A task-scoped
revision prevents combining responses after an artifact changes or a rerun advances.
Task navigation aborts in-flight requests and discards previous chart, selection,
units, table, download URL and stale asynchronous responses.

## API

All paths are under `/api/v1/projects/<project_id>/tasks/<task_key>/curves`:

| Path | Result |
| --- | --- |
| base | Column metadata, detected text format, limits and revision |
| `/data?time=c0&columns=c1,c2` | Shared sampled records, time extent and warning counts |
| `/data?...&start=1&end=2&limit=1600` | Refined range (limit: 100–2000) |
| `/point?...&at=1.25` | Nearest original record and exact text cells |
| `/raw?offset=0&count=100` | Original records, including malformed/non-numeric cells |
| `/download` | Original bytes as an attachment; parsing is not required |

`encoding`, `delimiter` and `revision` may accompany reads. JSON responses are
`no-store`; raw user text is inserted with `textContent`, not HTML. CSV is downloaded
as an attachment with `nosniff`. Source files are not modified or annotated by the
viewer. Spreadsheet formula cells remain original text, so use the usual care when
opening an untrusted raw CSV in a spreadsheet application.
