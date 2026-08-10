"""Locate the exact hop where run write-back stops being visible.

The evidence loop has four hops, and a break in any one of them looks identical
from the sheet ("the columns stayed empty"):

    1. field definitions   the three server columns exist and are active
    2. row matching        a matrix row shares the finished task's test id
    3. database write      custom_values actually carries the values
    4. delivery            the open Univer sheet receives them

This walks all four against the live database and says which one failed, so the
answer is not "it does not work" but "hop N failed, here is why".

Usage (from the project root, with the app's virtualenv active):

    python -m scripts.diagnose_writeback
    python -m scripts.diagnose_writeback --project 3
    python -m scripts.diagnose_writeback --project 3 --test-id TC-001

Read-only: it never writes to the database.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta

SERVER_FIELDS = ("exec_date", "executor", "version_label")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diagnose the run write-back loop against the live database.")
    parser.add_argument("--project", type=int, default=None,
                        help="Project id to inspect (default: every project "
                             "that has a finished task).")
    parser.add_argument("--test-id", default=None,
                        help="Only look at this logical test id.")
    parser.add_argument("--limit", type=int, default=5,
                        help="How many recent finished tasks to inspect.")
    args = parser.parse_args()

    from app import create_app
    from app.extensions import db
    from app.models import (FieldDefinition, Project, RowWriteback, Task,
                            TestItemRow, TestRunRecord)
    from app.services.lanmatrix import run_writeback_service as rws
    from app.collab import presence

    application = create_app()
    with application.app_context():
        dialect = db.engine.dialect.name
        print(f"database dialect : {dialect}")
        print(f"display timezone : "
              f"{application.config.get('LM_DISPLAY_TZ', '(unset)')}")

        q = Task.query.filter(Task.status == "finished")
        if args.project:
            q = q.filter(Task.project_id == args.project)
        if args.test_id:
            q = q.filter(Task.test_id == args.test_id)
        tasks = q.order_by(Task.id.desc()).limit(args.limit).all()

        if not tasks:
            print("\nNo finished tasks found. Run a task first, or widen "
                  "--project / --test-id.")
            return 1

        # ---- hop 1: field definitions ----------------------------------- #
        project_ids = sorted({t.project_id for t in tasks if t.project_id})
        print("\n=== hop 1: server field definitions ===")
        missing_by_project: dict[int, list[str]] = {}
        for pid in project_ids:
            defs = {d.field_key: d for d in FieldDefinition.query.filter_by(
                project_id=pid, sheet="test").all()}
            missing = [k for k in SERVER_FIELDS if k not in defs]
            inactive = [k for k in SERVER_FIELDS
                        if k in defs and not defs[k].is_active]
            deleted = [k for k in SERVER_FIELDS
                       if k in defs and getattr(defs[k], "deleted_at", None)]
            missing_by_project[pid] = missing
            project = db.session.get(Project, pid)
            name = project.name if project else "?"
            if missing or inactive or deleted:
                print(f"  project {pid} ({name}): MISSING={missing or '-'} "
                      f"INACTIVE={inactive or '-'} DELETED={deleted or '-'}")
                # Nothing seeds these columns, and the write-back stores values
                # in the row's JSON bag whether or not a column exists. So this
                # failure looks exactly like "the write-back did not run", even
                # though the data is there -- say so explicitly.
                print("    -> the run values are still stored on the row, but "
                      "with no column defined the sheet has nowhere to show "
                      "them. Add the missing field(s) on the project's field "
                      "settings page and they will appear.")
            else:
                print(f"  project {pid} ({name}): all three columns present "
                      f"and active")
                # Present-and-active is NOT the same as visible. A field created
                # by hand gets display_order = max + 1, so it lands at the far
                # right of a wide sheet -- past the viewport, while `result`
                # sits mid-sheet where people look. That reads as "the write-back
                # did not happen". Report the actual column position so the user
                # can tell "not written" from "written, off-screen".
                ordered = [d for d in sorted(
                    (d for d in FieldDefinition.query.filter_by(
                        project_id=pid, sheet="test").all()
                     if d.is_active and not getattr(d, "deleted_at", None)),
                    key=lambda d: (d.display_order or 0, d.id))]
                total = len(ordered)
                pos = {d.field_key: i + 1 for i, d in enumerate(ordered)}
                where = ", ".join(f"{k}=#{pos.get(k, '?')}"
                                  for k in SERVER_FIELDS)
                print(f"    column position out of {total}: {where}"
                      f"  (result=#{pos.get('result', '?')})")
                far = [k for k in SERVER_FIELDS if pos.get(k, 0) > total - 6]
                if far:
                    print("    -> these sit at the far right of the sheet. "
                          "Scroll right, or reorder them on the field settings "
                          "page, before concluding nothing was written.")

        # ---- hops 2 + 3: matching and the stored values ------------------ #
        print("\n=== hops 2 and 3: row matching and stored values ===")
        for task in tasks:
            print(f"\n  task #{task.id} key={task.task_key!r} "
                  f"test_id={task.test_id!r} project={task.project_id} "
                  f"finished_at={task.finished_at}")
            try:
                rows = rws._matching_rows(task.project_id, task.test_id)
            except Exception as exc:  # noqa: BLE001
                print(f"    hop 2 FAILED: row matching raised {type(exc).__name__}: {exc}")
                print("    -> this is the bug that silently swallowed the "
                      "write-back; make sure you are running the fixed build.")
                continue
            if not rows:
                print("    hop 2 FAILED: no matrix row has this test id.")
                sample = (TestItemRow.query
                          .filter_by(project_id=task.project_id, sheet="test",
                                     deleted_at=None)
                          .limit(3).all())
                for r in sample:
                    from app.services.lanmatrix import silver_json_export as sje
                    print(f"      e.g. row uuid={r.uuid} case_id={r.case_id!r} "
                          f"logical_id={sje.row_test_id(r)!r}")
                print("    -> the task's test id must equal the row's test_id "
                      "field (or its case_id).")
                continue
            print(f"    hop 2 ok: {len(rows)} row(s) matched")
            for row in rows:
                cv = row.custom_values or {}
                present = {k: cv.get(k) for k in SERVER_FIELDS}
                blank = [k for k, v in present.items() if not v]
                print(f"    row {row.uuid}: result={row.result!r} {present}")
                if blank:
                    print(f"      hop 3 INCOMPLETE: blank {blank}")
                    if missing_by_project.get(task.project_id):
                        print("      -> the column is not defined on this "
                              "project, so the value had nowhere to go.")
                else:
                    print("      hop 3 ok: all three values are in the database")

            records = (TestRunRecord.query
                       .filter_by(project_id=task.project_id,
                                  task_key=task.task_key or "")
                       .all())
            print(f"    run records appended: {len(records)}"
                  + (f" (latest version={records[-1].model_version!r}, "
                     f"executor={records[-1].executor_name!r}, "
                     f"on={records[-1].executed_on!r})" if records else ""))

        # ---- hop 4: delivery to the open sheet --------------------------- #
        print("\n=== hop 4: delivery to the open sheet ===")
        for pid in project_ids:
            active = presence.is_collab_active(pid)
            pending = (RowWriteback.query
                       .filter_by(project_id=pid)
                       .filter(RowWriteback.applied_at.is_(None))
                       .count())
            stale_cut = datetime.utcnow() - timedelta(minutes=5)
            stale = (RowWriteback.query
                     .filter_by(project_id=pid)
                     .filter(RowWriteback.applied_at.is_(None))
                     .filter(RowWriteback.created_at < stale_cut)
                     .count())
            print(f"  project {pid}: collab_active={active} "
                  f"pending_writebacks={pending} older_than_5min={stale}")
            if active and stale:
                print("    -> the collab server is not draining its queue. "
                      "Values are safe in the database but will not reach the "
                      "open sheet until it does. Restart the collab process.")
            elif not active:
                print("    -> nobody is in a live room, so values are read "
                      "straight from the database on the next page load. "
                      "Reload the sheet to see them.")
            else:
                print("    -> queue is being drained normally.")

        print("\nIf every hop is ok but the sheet still looks empty, the "
              "columns may simply be scrolled out of view or hidden in the "
              "sheet's column settings.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
