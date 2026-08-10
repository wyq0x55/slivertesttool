# Review sign-off, in-app notifications, and the project dashboard

This document covers the changes delivered in phases P3-P5. It explains the
counting rules the dashboard depends on (these are the part most likely to be
"fixed" into something wrong by a later change) and lists the new
configuration, schema and endpoints.

---

## 1. Review sign-off

### 1.1 The asymmetry is the feature

Two verdicts can require sign-off, and they are deliberately not treated the
same way:

| Verdict      | Bulk approve | Note required | Default policy |
|--------------|--------------|---------------|----------------|
| `PASS`       | yes          | no            | off            |
| `Untestable` | **no**       | **yes**       | **on**         |

`PASS` is bulk-approvable because a regression sweep turns hundreds of cases
green at once. Forcing one click each does not produce diligence, it produces
rubber-stamping — the paperwork of review without the review.

`Untestable` is the opposite. Each one silently removes a case from the
evidence base, which is exactly the change that should never happen quietly.
It is approved one at a time and demands a written note.

`FAIL` and `ERROR` are **not** reviewable states. A failing case is already
visible as a failure; requiring someone to countersign it adds a queue without
adding information.

### 1.2 Policy is per project

`Project.review_required_on` (JSON) holds `{"pass": bool, "untestable": bool}`,
defaulting to `Project.REVIEW_DEFAULTS`. A safety-critical project reviews
every `PASS`; an exploratory one reviews nothing. Edited on the
**成员管理** settings tab — the same page as reviewer roles, because "who
reviews" and "what must be reviewed" are two halves of one decision. Split
across tabs, an admin can appoint reviewers and never route anything to them.

Requires `project.edit` (i.e. `project_admin`). Readers can view the page but
the panel stays hidden — showing them disabled controls advertises a capability
they do not have.

### 1.3 Review state lives on its own columns

`TestItemRow` gains:

```
review_status     ""|pending|approved|rejected
review_verdict    the verdict the request was raised for
review_note       reviewer's written opinion
reviewer_id       assigned reviewer
reviewed_by_id    who actually decided
reviewed_at       when
```

`review_verdict` is stored separately from `row.result` on purpose: `result` is
overwritten by the next run, so a decision recorded against it would silently
re-point at a different outcome than the one that was actually reviewed.

`workflow_status` is **not** reused. Its default is `"Draft"`, which would make
every newly imported row look like it was mid-review.

### 1.4 Behaviour worth knowing

- `request_review` is **idempotent**. Re-running a case that is already pending
  for the same verdict does not re-raise, so a nightly regression does not spam
  the assigned reviewer.
- A reviewer **persists across runs**. A case keeps its owner instead of
  becoming unassigned work nobody picks up.
- **A request always has a recipient.** `resolve_reviewer` walks
  `explicit -> row.reviewer_id -> project.default_reviewer_id -> project.owner_id`.
  Before the last two links existed, the chain ended at `row.reviewer_id`, which
  is only set by assigning a reviewer to that row by hand — so automatic
  requests resolved to `None`, no notification went out, and `pending_for`
  (which filters by reviewer) returned an empty queue. Turning review on
  achieved nothing, silently.
- **One default reviewer per project, not a pool.** A request addressed to
  everybody is owned by nobody. `Project.default_reviewer_id` is set from the
  policy panel and must be a project member or the owner (the API returns 400
  otherwise); the reviewer may reassign.
- `decide_bulk` **skips** rows it may not act on rather than failing the whole
  batch. One ineligible row in a selection of two hundred should not discard
  the other 199 decisions.
- The review queue exposes the row's `description`, because when reviewing an
  `Untestable` the stated reason *is* the thing under review.

---

## 2. In-app notifications

### 2.1 Notification delivery must never break the thing it reports

`notification_service.notify` wraps its whole body in `try/except` and defaults
to `commit=False`. A notification is a side effect of work, and a failure to
announce a finished test run must not roll back the record of that run.

### 2.2 Noise control

- **Self-notification is suppressed.** You do not get told about your own
  actions.
- **One event, one row (collapsing is off by default).** Grouping used to fold a
  whole project's events into a single row rendered as `×2`, `×50`. A merged row
  carries exactly one `link_url`, so clicking it opened one arbitrary member and
  the rest were announced but unreachable — the user could see that something
  else had happened with no way to find out what. `LM_NOTIFY_GROUP_SECONDS` now
  defaults to `0`, and the default `group_key` is `type:project:ref_id`, i.e.
  unique per referenced object. Setting the window above zero re-enables merging,
  but only for literally the same event delivered twice.
- **Assigned-review notifications deep-link to the case**
  (`review_item_link` → `/lanmatrix/projects/<id>?row=<uuid>&from=workspace`),
  not to the queue, so the reviewer does not have to search for the row the
  notification already named.
- **`purge_old` only deletes read notifications.** Unread items are somebody's
  outstanding work; ageing them out silently loses it.
- **Archived rows do not revive.** The collapsing window skips rows with
  `archived_at` set, so an item the user filed away cannot be resurrected by the
  next occurrence of the same event.

### 2.3 Read, archive, retention

Three distinct states, because "I saw it", "I dealt with it" and "it never
happened" are three different claims:

| State | Column | Meaning |
| --- | --- | --- |
| unread | `is_read = false` | outstanding work |
| read | `read_at` set | seen, still listed in 历史 |
| archived | `archived_at` set | filed away by the user |

- **Closing the dropdown no longer marks everything read.** Treating a glance as
  "read" is what made notifications vanish on sight and then pile straight back
  up: the user never got to act on what they saw. Marking read is now always
  explicit — clicking an item, 全部已读, or archiving.
- **Retention ages rows by `read_at`, not `created_at`.** Ageing by creation
  time deletes an old notification the moment it is finally opened.
  `purge_old` uses `coalesce(read_at, created_at)` and defaults to
  **30 days** (`LM_NOTIFY_RETENTION_DAYS`).
- **清空历史 never touches unread rows.**

API: `GET /me/notifications?scope=unread|history|all`,
`POST /me/notifications/read`, `POST /me/notifications/archive`,
`POST /me/notifications/clear_history`.

### 2.4 Links

Notification targets are built by `notification_service.task_link` /
`review_queue_link` / `project_link` rather than written out at each call site.
The page blueprint is mounted at `/lanmatrix`, and hand-written paths had
dropped the prefix (`/projects/<id>/tasks`) or pointed at routes that never
existed (`/workspace/reviews`, `/projects/<id>/matrix`) — every such link 404'd.
`task_link` emits `?task=<task_key>`, which `project_tasks.js` consumes as a
deep link and expands the referenced task.

### 2.5 Polling

The badge has its own lightweight endpoint (`unread_count`) so the 30-second
poll does not drag the full payload. Polling stops while `document.hidden`.

Configuration: `LM_NOTIFY_POLL_SECONDS`, `LM_NOTIFY_COLLAPSE_SECONDS`,
`LM_NOTIFY_RETENTION_DAYS` (default 30), `LM_NOTIFY_PAGE_SIZE`.

---

## 3. Dashboard

### 3.1 The counting rules

These identities always hold, and the tests in `tests/test_dashboard.py`
enforce them:

```
total    = out_of_scope + planned
planned  = not_run + executed
executed = passed + failed + errored + untestable
```

Concretely:

| Bucket         | Definition |
|----------------|------------|
| `total`        | rows on the `test` sheet with `deleted_at IS NULL` |
| `out_of_scope` | `workflow_status == "Archived"` — **only** this |
| `planned`      | everything else |
| `executed`     | rows whose current result is pass/fail/error/untestable |
| `not_run`      | planned rows with no result yet |

Percentages are relative to **`planned`, not `total`**. Counting archived rows
in the denominator understates real progress on work that was deliberately
descoped.

### 3.2 Five decisions that look like details but are not

**`cancelled` is not executed.** A cancelled run produced no evidence.
Counting it would inflate progress with work that did not happen. It is folded
back into `not_run`.

**`out_of_scope` recognises only an explicit `Archived` status.** Any broader
exclusion rule would let a project improve its completion percentage by
reclassifying inconvenient cases.

**`executed` is derived from the row's current verdict, not from run history.**
A case re-run from FAIL to PASS is one case, not two.

**`trend` counts each case on the date of its *first* execution.** Running the
same case ten times is not ten units of progress. This is what keeps the
cumulative curve monotonic.

**`by_version` takes the last result per case per version.** Versions are
ordered by first appearance, not sorted — string sorting puts `10.0` before
`9.0`.

Runs with no version label get their own `(未标注)` bucket rather than being
dropped. A project that tested before it adopted version labels still did that
work, and discarding it would make the chart disagree with the summary card.

### 3.3 One request, not four

`GET /api/v1/projects/<id>/dashboard` returns every section in one payload.
Four independent requests would let the progress ring and the review funnel be
computed at different moments, so the page could contradict itself.

Large projects are **folded, not truncated**: `trend` keeps a `baseline` so the
curve does not appear to start at zero, and `by_version` folds old versions
into an "other" bucket instead of dropping them.

---

## 4. Charts: why ECharts is vendored

`app/static/vendor/charts/echarts.min.js` is committed as a prebuilt bundle
(~1.0 MB) rather than built from source through Vite.

The reason is specific: the supplied `echarts` package did not include its
`zrender` and `tslib` dependencies, so a tree-shaken build cannot be produced.
Shipping a build config that cannot run is worse than shipping a working file.

To slim it down later: `npm i echarts` locally, build a tree-shaken bundle with
only the used chart/component modules, and replace the vendored file. Nothing
else has to change — `charts_theme.js` only needs `window.echarts`.

Only the dashboard page loads it. For scale: the Univer bundle already shipped
is ~11.2 MB.

### 4.1 Theming

Charts read **`--chart-*`** tokens, never `--ok` / `--warn` / `--danger`
directly.

The semantic tokens are tuned for small elements — pill text, dots, 1px borders
— where the `--*-soft` background carries the meaning. A chart is a large fill
sitting directly on `--surface`. In dark mode `--ok #12a150` on `#141518` does
not have enough contrast. The `--chart-*` layer exists so dark mode can lift
those fills without changing how any existing pill looks.

A chart that references `--ok` directly will look correct in light mode and
degrade silently in dark mode.

ECharts bakes its theme at `init()`, so a theme switch requires dispose +
rebuild. `charts_theme.js` watches `data-theme` and dispatches
`lm-chart-rebuilt` so pages can rebind their instance handles. Without the
rebuild, a dark card keeps light-mode text.

---

## 5. New endpoints

```
GET  /api/v1/me/reviews
GET  /api/v1/me/notifications
GET  /api/v1/me/notifications/unread_count
POST /api/v1/me/notifications/read
GET  /api/v1/projects/<id>/reviews
POST /api/v1/projects/<id>/items/<row_uuid>/review
POST /api/v1/projects/<id>/items/<row_uuid>/reviewer
POST /api/v1/projects/<id>/reviews/bulk
PUT  /api/v1/projects/<id>/review_policy
GET  /api/v1/projects/<id>/dashboard
```

Page routes:

```
GET /lanmatrix/projects/<id>/dashboard
```

All mutating endpoints require the `X-CSRF-Token` header matching
`session["csrf_token"]`.

---

## 6. Navigation

The project rail now carries four destinations: 仪表盘 / 测试矩阵 / 任务与运行
/ 设置.

仪表盘 gets a rail slot rather than a tab inside 测试矩阵 because it answers a
different question ("how far along are we") for a different reader — typically
one who never opens the matrix at all. Behind a tab it would go unread, which
is the exact failure it exists to fix.

### 6.1 The workspace is tabbed, not stacked

工作台 used to render 最近任务, 待我审核 and 我的项目 one under the other, so the
third list was only reachable after scrolling past up to 200 task rows and every
pending review. They are now three panes behind the same `.tabs` strip the
project 设置 pages use, and the active pane lives in `?view=`
(`tasks` | `reviews` | `projects`; `tasks` is the default and is omitted from the
URL). Notification links already used `?view=reviews`, so they keep working and
now land on the queue directly instead of scrolling to it.

Each list is paged client-side by `static/js/lanmatrix/pager.js` — 20 rows for
tasks and reviews, 9 cards for projects. The data is already fetched and capped
server-side, so paging costs no extra round trip. 全选 in the review queue still
covers the *whole* queue rather than the visible page: the label promises all of
the reviewer's pending work, and selection survives page switches.

Links from the personal workspace into a project carry `?from=workspace`, and
those pages then show a **返回我的工作台** link. This is a real URL rather than
`history.back()`, because a user who arrived via a pasted link would otherwise
be thrown out of the application.

---

## 7. Known gaps

- `project_tasks.js` still implements its own cancel/delete/retest verbs
  instead of delegating to `LMTaskActions`. Both work today, but they can
  drift. Consolidating them is the next cleanup.
- The project page has no embedded review panel; review work is done from the
  personal workspace queue or the matrix.
