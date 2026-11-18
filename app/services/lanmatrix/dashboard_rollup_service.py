"""Incremental O(1) dashboard rollup maintenance for project run history."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from ...extensions import db
from ...models.lanmatrix import (
    DashboardCaseFirstRun,
    DashboardCaseVersionLatest,
    DashboardDailyRollup,
    DashboardVersionRollup,
    Project,
    TestRunRecord,
)

OUTCOMES = ("pass", "fail", "error", "untestable", "cancelled")
EXECUTED_OUTCOMES = ("pass", "fail", "error", "untestable")
UNLABELLED_VERSION = "(未标注)"

_ROLLUP_TABLES = (
    "lm_dashboard_case_first_runs",
    "lm_dashboard_daily_rollups",
    "lm_dashboard_case_version_latest",
    "lm_dashboard_version_rollups",
)
_OUTCOME_COLUMNS = {
    "pass": "pass_count",
    "fail": "fail_count",
    "error": "error_count",
    "untestable": "untestable_count",
    "cancelled": "cancelled_count",
}
_CHUNK_SIZE = 400
_LISTENERS_REGISTERED = False


@dataclass(frozen=True)
class _RecordItem:
    record_id: int
    project_id: int
    case_key: str
    outcome: str
    version_label: str
    executed_at: datetime
    executed_on: str


def case_identity_values(row_uuid: str | None, test_id: str | None) -> str:
    """Compute canonical case identity matching ``_case_key_expr``."""
    if row_uuid is not None and row_uuid != "":
        return str(row_uuid)
    return f"tid:{test_id or ''}"


def case_identity_for_record(record: TestRunRecord) -> str:
    """Return the canonical case identity key for a ``TestRunRecord``."""
    return case_identity_values(record.row_uuid, record.test_id)


def version_label_for_value(model_version: str | None) -> str:
    """Normalize ``model_version`` identically to ``by_version``."""
    return (model_version or "").strip() or UNLABELLED_VERSION


def normalize_executed_on(value: Any) -> str:
    """Normalize a calendar date column value to ``YYYY-MM-DD`` or empty string."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def normalize_executed_at(value: Any) -> datetime:
    """Normalize an execution timestamp to naive UTC ``datetime``."""
    if value is None:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    raise TypeError(f"Unsupported executed_at value: {value!r}")


def _rollup_tables_ready(conn: Connection) -> bool:
    dialect_name = conn.dialect.name
    if dialect_name == "postgresql":
        ready = conn.execute(
            text(
                "SELECT to_regclass('lm_dashboard_case_first_runs') IS NOT NULL "
                "AND to_regclass('lm_dashboard_daily_rollups') IS NOT NULL "
                "AND to_regclass('lm_dashboard_case_version_latest') IS NOT NULL "
                "AND to_regclass('lm_dashboard_version_rollups') IS NOT NULL"
            )
        ).scalar()
        return bool(ready)
    if dialect_name == "sqlite":
        table_count = conn.execute(
            text(
                "SELECT count(*) FROM sqlite_master "
                "WHERE type = 'table' AND name IN ("
                "'lm_dashboard_case_first_runs', "
                "'lm_dashboard_daily_rollups', "
                "'lm_dashboard_case_version_latest', "
                "'lm_dashboard_version_rollups')"
            )
        ).scalar()
        return int(table_count or 0) == len(_ROLLUP_TABLES)
    from sqlalchemy import inspect

    inspector = inspect(conn)
    return all(inspector.has_table(name) for name in _ROLLUP_TABLES)


def _acquire_project_lock(conn: Connection, project_id: int) -> None:
    if conn.dialect.name == "postgresql":
        conn.execute(
            text("SELECT pg_catalog.pg_advisory_xact_lock(19536, :pid)"),
            {"pid": int(project_id)},
        )


def _to_record_item(record: TestRunRecord | _RecordItem) -> _RecordItem | None:
    if isinstance(record, _RecordItem):
        return record
    if record.id is None or record.project_id is None:
        return None
    return _RecordItem(
        record_id=int(record.id),
        project_id=int(record.project_id),
        case_key=case_identity_for_record(record),
        outcome=(record.outcome or "").strip(),
        version_label=version_label_for_value(record.model_version),
        executed_at=normalize_executed_at(record.executed_at),
        executed_on=normalize_executed_on(record.executed_on),
    )


def _chunked(values: Sequence[Any], size: int = _CHUNK_SIZE) -> Iterable[Sequence[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _apply_project_batch(
    conn: Connection,
    project_id: int,
    items: Sequence[_RecordItem],
) -> None:
    if not items:
        return
    _acquire_project_lock(conn, project_id)

    first_table = DashboardCaseFirstRun.__table__
    daily_table = DashboardDailyRollup.__table__
    latest_table = DashboardCaseVersionLatest.__table__
    version_table = DashboardVersionRollup.__table__

    ordered_items = sorted(items, key=lambda entry: entry.record_id)

    exec_items = [
        entry
        for entry in ordered_items
        if entry.outcome in EXECUTED_OUTCOMES and entry.executed_on != ""
    ]
    if exec_items:
        exec_case_keys = sorted({entry.case_key for entry in exec_items})
        existing_first: dict[str, str] = {}
        for chunk in _chunked(exec_case_keys):
            rows = conn.execute(
                select(first_table.c.case_key, first_table.c.first_executed_on).where(
                    first_table.c.project_id == project_id,
                    first_table.c.case_key.in_(chunk),
                )
            ).all()
            for case_key, first_day in rows:
                existing_first[str(case_key)] = str(first_day)

        current_first = dict(existing_first)
        daily_first_delta: dict[str, int] = defaultdict(int)
        daily_runs_delta: dict[str, int] = defaultdict(int)

        for entry in exec_items:
            daily_runs_delta[entry.executed_on] += 1
            previous_day = current_first.get(entry.case_key)
            if previous_day is None:
                current_first[entry.case_key] = entry.executed_on
                daily_first_delta[entry.executed_on] += 1
            elif entry.executed_on < previous_day:
                current_first[entry.case_key] = entry.executed_on
                daily_first_delta[previous_day] -= 1
                daily_first_delta[entry.executed_on] += 1

        first_inserts = [
            {
                "project_id": project_id,
                "case_key": case_key,
                "first_executed_on": first_day,
            }
            for case_key, first_day in current_first.items()
            if case_key not in existing_first
        ]
        for chunk in _chunked(first_inserts):
            conn.execute(first_table.insert(), list(chunk))

        for case_key, first_day in current_first.items():
            if case_key in existing_first and existing_first[case_key] != first_day:
                conn.execute(
                    update(first_table)
                    .where(
                        first_table.c.project_id == project_id,
                        first_table.c.case_key == case_key,
                    )
                    .values(first_executed_on=first_day)
                )

        touched_days = sorted(set(daily_first_delta) | set(daily_runs_delta))
        existing_daily: dict[str, tuple[int, int]] = {}
        for chunk in _chunked(touched_days):
            rows = conn.execute(
                select(
                    daily_table.c.executed_on,
                    daily_table.c.first_case_count,
                    daily_table.c.total_runs,
                ).where(
                    daily_table.c.project_id == project_id,
                    daily_table.c.executed_on.in_(chunk),
                )
            ).all()
            for day, first_cnt, runs_cnt in rows:
                existing_daily[str(day)] = (int(first_cnt or 0), int(runs_cnt or 0))

        daily_inserts = [
            {
                "project_id": project_id,
                "executed_on": day,
                "first_case_count": daily_first_delta[day],
                "total_runs": daily_runs_delta[day],
            }
            for day in touched_days
            if day not in existing_daily
        ]
        for chunk in _chunked(daily_inserts):
            conn.execute(daily_table.insert(), list(chunk))

        for day in touched_days:
            if day not in existing_daily:
                continue
            delta_first = daily_first_delta[day]
            delta_runs = daily_runs_delta[day]
            if delta_first == 0 and delta_runs == 0:
                continue
            prev_first, prev_runs = existing_daily[day]
            conn.execute(
                update(daily_table)
                .where(
                    daily_table.c.project_id == project_id,
                    daily_table.c.executed_on == day,
                )
                .values(
                    first_case_count=prev_first + delta_first,
                    total_runs=prev_runs + delta_runs,
                )
            )

    ver_items = [entry for entry in ordered_items if entry.outcome in OUTCOMES]
    if ver_items:
        version_labels = sorted({entry.version_label for entry in ver_items})
        ver_case_keys = sorted({entry.case_key for entry in ver_items})

        existing_ver: dict[str, dict[str, Any]] = {}
        for chunk in _chunked(version_labels):
            rows = conn.execute(
                select(
                    version_table.c.version_label,
                    version_table.c.first_seen_at,
                    version_table.c.first_seen_id,
                    version_table.c.pass_count,
                    version_table.c.fail_count,
                    version_table.c.error_count,
                    version_table.c.untestable_count,
                    version_table.c.cancelled_count,
                ).where(
                    version_table.c.project_id == project_id,
                    version_table.c.version_label.in_(chunk),
                )
            ).all()
            for (
                ver_lbl,
                seen_at,
                seen_id,
                pass_cnt,
                fail_cnt,
                err_cnt,
                untest_cnt,
                cancel_cnt,
            ) in rows:
                existing_ver[str(ver_lbl)] = {
                    "first_seen_at": normalize_executed_at(seen_at),
                    "first_seen_id": int(seen_id),
                    "pass": int(pass_cnt or 0),
                    "fail": int(fail_cnt or 0),
                    "error": int(err_cnt or 0),
                    "untestable": int(untest_cnt or 0),
                    "cancelled": int(cancel_cnt or 0),
                    "is_new": False,
                    "dirty": False,
                }

        existing_latest: dict[tuple[str, str], tuple[str, datetime, int]] = {}
        for chunk in _chunked(ver_case_keys):
            rows = conn.execute(
                select(
                    latest_table.c.version_label,
                    latest_table.c.case_key,
                    latest_table.c.outcome,
                    latest_table.c.executed_at,
                    latest_table.c.record_id,
                ).where(
                    latest_table.c.project_id == project_id,
                    latest_table.c.version_label.in_(version_labels),
                    latest_table.c.case_key.in_(chunk),
                )
            ).all()
            for ver_lbl, case_key, outcome, exec_at, rec_id in rows:
                existing_latest[(str(ver_lbl), str(case_key))] = (
                    str(outcome),
                    normalize_executed_at(exec_at),
                    int(rec_id),
                )

        current_latest = dict(existing_latest)
        for entry in ver_items:
            ver_state = existing_ver.get(entry.version_label)
            if ver_state is None:
                ver_state = {
                    "first_seen_at": entry.executed_at,
                    "first_seen_id": entry.record_id,
                    "pass": 0,
                    "fail": 0,
                    "error": 0,
                    "untestable": 0,
                    "cancelled": 0,
                    "is_new": True,
                    "dirty": True,
                }
                existing_ver[entry.version_label] = ver_state
            elif (entry.executed_at, entry.record_id) < (
                ver_state["first_seen_at"],
                ver_state["first_seen_id"],
            ):
                ver_state["first_seen_at"] = entry.executed_at
                ver_state["first_seen_id"] = entry.record_id
                ver_state["dirty"] = True

            pair = (entry.version_label, entry.case_key)
            prev_latest = current_latest.get(pair)
            if prev_latest is None:
                current_latest[pair] = (
                    entry.outcome,
                    entry.executed_at,
                    entry.record_id,
                )
                ver_state[entry.outcome] += 1
                ver_state["dirty"] = True
            else:
                prev_outcome, prev_at, prev_id = prev_latest
                if (entry.executed_at, entry.record_id) >= (prev_at, prev_id):
                    if entry.outcome != prev_outcome:
                        ver_state[prev_outcome] -= 1
                        ver_state[entry.outcome] += 1
                        ver_state["dirty"] = True
                    current_latest[pair] = (
                        entry.outcome,
                        entry.executed_at,
                        entry.record_id,
                    )

        latest_inserts = [
            {
                "project_id": project_id,
                "version_label": pair[0],
                "case_key": pair[1],
                "outcome": latest_val[0],
                "executed_at": latest_val[1],
                "record_id": latest_val[2],
            }
            for pair, latest_val in current_latest.items()
            if pair not in existing_latest
        ]
        for chunk in _chunked(latest_inserts):
            conn.execute(latest_table.insert(), list(chunk))

        for pair, latest_val in current_latest.items():
            if pair in existing_latest and existing_latest[pair] != latest_val:
                conn.execute(
                    update(latest_table)
                    .where(
                        latest_table.c.project_id == project_id,
                        latest_table.c.version_label == pair[0],
                        latest_table.c.case_key == pair[1],
                    )
                    .values(
                        outcome=latest_val[0],
                        executed_at=latest_val[1],
                        record_id=latest_val[2],
                    )
                )

        ver_inserts = [
            {
                "project_id": project_id,
                "version_label": ver_lbl,
                "first_seen_at": ver_state["first_seen_at"],
                "first_seen_id": ver_state["first_seen_id"],
                "pass_count": ver_state["pass"],
                "fail_count": ver_state["fail"],
                "error_count": ver_state["error"],
                "untestable_count": ver_state["untestable"],
                "cancelled_count": ver_state["cancelled"],
            }
            for ver_lbl, ver_state in existing_ver.items()
            if ver_state["is_new"]
        ]
        for chunk in _chunked(ver_inserts):
            conn.execute(version_table.insert(), list(chunk))

        for ver_lbl, ver_state in existing_ver.items():
            if not ver_state["is_new"] and ver_state["dirty"]:
                conn.execute(
                    update(version_table)
                    .where(
                        version_table.c.project_id == project_id,
                        version_table.c.version_label == ver_lbl,
                    )
                    .values(
                        first_seen_at=ver_state["first_seen_at"],
                        first_seen_id=ver_state["first_seen_id"],
                        pass_count=ver_state["pass"],
                        fail_count=ver_state["fail"],
                        error_count=ver_state["error"],
                        untestable_count=ver_state["untestable"],
                        cancelled_count=ver_state["cancelled"],
                    )
                )


def apply_run_records(
    records: Iterable[TestRunRecord | _RecordItem],
    *,
    conn: Connection | None = None,
) -> None:
    """Apply newly persisted ``TestRunRecord`` instances to rollup tables."""
    pending_orm: list[TestRunRecord] = []
    by_project: dict[int, list[_RecordItem]] = defaultdict(list)

    for record in records:
        if isinstance(record, TestRunRecord):
            if getattr(record, "_rollup_synced", False):
                continue
            item = _to_record_item(record)
            if item is None:
                continue
            pending_orm.append(record)
            by_project[item.project_id].append(item)
        else:
            item = _to_record_item(record)
            if item is not None:
                by_project[item.project_id].append(item)

    if not by_project:
        return

    active_conn = conn if conn is not None else db.session.connection()
    if not _rollup_tables_ready(active_conn):
        return

    for project_id in sorted(by_project):
        _apply_project_batch(active_conn, project_id, by_project[project_id])

    for record in pending_orm:
        record._rollup_synced = True


def sync_record(record: TestRunRecord) -> None:
    """Flush ``db.session`` if needed and sync ``record`` into dashboard rollups."""
    if getattr(record, "_rollup_synced", False):
        return
    if record.id is None:
        db.session.flush()
    if not getattr(record, "_rollup_synced", False):
        apply_run_records([record], conn=db.session.connection())


def clear_project_rollups(
    project_id: int,
    *,
    conn: Connection | None = None,
) -> None:
    """Delete all rollup rows belonging to ``project_id``."""
    active_conn = conn if conn is not None else db.session.connection()
    if not _rollup_tables_ready(active_conn):
        return
    _acquire_project_lock(active_conn, project_id)
    for model_cls in (
        DashboardCaseFirstRun,
        DashboardDailyRollup,
        DashboardCaseVersionLatest,
        DashboardVersionRollup,
    ):
        tbl = model_cls.__table__
        active_conn.execute(delete(tbl).where(tbl.c.project_id == project_id))


def rebuild_project_rollups(
    project_id: int,
    *,
    conn: Connection | None = None,
) -> None:
    """Rebuild all four rollup tables for ``project_id`` from ``lm_test_run_records``."""
    active_conn = conn if conn is not None else db.session.connection()
    if not _rollup_tables_ready(active_conn):
        return

    clear_project_rollups(project_id, conn=active_conn)

    project_table = Project.__table__
    project_exists = active_conn.execute(
        select(project_table.c.id).where(project_table.c.id == project_id)
    ).scalar()
    if project_exists is None:
        return

    record_table = TestRunRecord.__table__
    last_id = 0
    batch_limit = 2000
    while True:
        rows = active_conn.execute(
            select(
                record_table.c.id,
                record_table.c.project_id,
                record_table.c.row_uuid,
                record_table.c.test_id,
                record_table.c.outcome,
                record_table.c.model_version,
                record_table.c.executed_at,
                record_table.c.executed_on,
            )
            .where(
                record_table.c.project_id == project_id,
                record_table.c.id > last_id,
            )
            .order_by(record_table.c.id.asc())
            .limit(batch_limit)
        ).all()
        if not rows:
            break
        items = [
            _RecordItem(
                record_id=int(rec_id),
                project_id=int(proj_id),
                case_key=case_identity_values(row_uuid, test_id),
                outcome=(outcome or "").strip(),
                version_label=version_label_for_value(model_version),
                executed_at=normalize_executed_at(exec_at),
                executed_on=normalize_executed_on(exec_on),
            )
            for rec_id, proj_id, row_uuid, test_id, outcome, model_version, exec_at, exec_on in rows
        ]
        _apply_project_batch(active_conn, project_id, items)
        last_id = int(rows[-1][0])


def backfill_all_rollups(conn: Connection | None = None) -> int:
    """Rebuild rollups for every live project present in ``lm_test_run_records``."""
    active_conn = conn if conn is not None else db.session.connection()
    if not _rollup_tables_ready(active_conn):
        return 0
    record_table = TestRunRecord.__table__
    project_table = Project.__table__
    project_ids = (
        active_conn.execute(
            select(func.distinct(record_table.c.project_id))
            .select_from(
                record_table.join(
                    project_table,
                    project_table.c.id == record_table.c.project_id,
                )
            )
            .order_by(record_table.c.project_id.asc())
        )
        .scalars()
        .all()
    )
    for project_id in project_ids:
        rebuild_project_rollups(int(project_id), conn=active_conn)
    return len(project_ids)


def _on_after_flush(session: Session, _flush_context: Any) -> None:
    deleted_project_ids = {
        int(obj.id)
        for obj in session.deleted
        if isinstance(obj, Project) and obj.id is not None
    }
    rebuild_pids = {
        int(obj.project_id)
        for obj in session.deleted
        if isinstance(obj, TestRunRecord)
        and obj.project_id is not None
        and int(obj.project_id) not in deleted_project_ids
    }
    for obj in session.dirty:
        if (
            isinstance(obj, TestRunRecord)
            and obj.project_id is not None
            and session.is_modified(obj, include_collections=False)
            and int(obj.project_id) not in deleted_project_ids
        ):
            rebuild_pids.add(int(obj.project_id))

    new_records = [
        obj
        for obj in session.new
        if isinstance(obj, TestRunRecord)
        and not getattr(obj, "_rollup_synced", False)
        and obj.project_id is not None
        and int(obj.project_id) not in deleted_project_ids
    ]

    if not deleted_project_ids and not rebuild_pids and not new_records:
        return

    conn = session.connection()
    if not _rollup_tables_ready(conn):
        return

    for project_id in sorted(deleted_project_ids):
        clear_project_rollups(project_id, conn=conn)

    for project_id in sorted(rebuild_pids):
        rebuild_project_rollups(project_id, conn=conn)
        for rec in new_records:
            if int(rec.project_id) == project_id:
                rec._rollup_synced = True

    remaining_new = [
        rec for rec in new_records if not getattr(rec, "_rollup_synced", False)
    ]
    if remaining_new:
        apply_run_records(remaining_new, conn=conn)


def _on_after_soft_rollback(session: Session, _previous_transaction: Any) -> None:
    for obj in session.identity_map.values():
        if isinstance(obj, TestRunRecord) and getattr(obj, "_rollup_synced", False):
            obj._rollup_synced = False


def _on_do_orm_execute(orm_execute_state: Any) -> Any:
    target_table = getattr(orm_execute_state.statement, "table", None)
    if target_table is None:
        return None

    table_name = target_table.name
    if table_name == "lm_test_run_records":
        conn = orm_execute_state.session.connection()
        if not _rollup_tables_ready(conn):
            return None
        record_table = TestRunRecord.__table__
        if orm_execute_state.is_insert:
            max_id = int(
                conn.execute(
                    select(func.coalesce(func.max(record_table.c.id), 0))
                ).scalar()
                or 0
            )
            result = orm_execute_state.invoke_statement()
            inserted_rows = conn.execute(
                select(
                    record_table.c.id,
                    record_table.c.project_id,
                    record_table.c.row_uuid,
                    record_table.c.test_id,
                    record_table.c.outcome,
                    record_table.c.model_version,
                    record_table.c.executed_at,
                    record_table.c.executed_on,
                )
                .where(record_table.c.id > max_id)
                .order_by(record_table.c.id.asc())
            ).all()
            if inserted_rows:
                items = [
                    _RecordItem(
                        record_id=int(rec_id),
                        project_id=int(proj_id),
                        case_key=case_identity_values(row_uuid, test_id),
                        outcome=(outcome or "").strip(),
                        version_label=version_label_for_value(model_version),
                        executed_at=normalize_executed_at(exec_at),
                        executed_on=normalize_executed_on(exec_on),
                    )
                    for rec_id, proj_id, row_uuid, test_id, outcome, model_version, exec_at, exec_on in inserted_rows
                ]
                apply_run_records(items, conn=conn)
            return result

        if orm_execute_state.is_delete or orm_execute_state.is_update:
            daily_table = DashboardDailyRollup.__table__
            ver_table = DashboardVersionRollup.__table__
            candidate_pids = set(
                conn.execute(select(func.distinct(daily_table.c.project_id)))
                .scalars()
                .all()
            ) | set(
                conn.execute(select(func.distinct(ver_table.c.project_id)))
                .scalars()
                .all()
            )
            if orm_execute_state.is_update:
                candidate_pids |= set(
                    conn.execute(select(func.distinct(record_table.c.project_id)))
                    .scalars()
                    .all()
                )
            result = orm_execute_state.invoke_statement()
            for project_id in sorted(int(pid) for pid in candidate_pids if pid is not None):
                rebuild_project_rollups(project_id, conn=conn)
            return result

    if table_name == "lm_projects" and orm_execute_state.is_delete:
        conn = orm_execute_state.session.connection()
        if not _rollup_tables_ready(conn):
            return None
        result = orm_execute_state.invoke_statement()
        project_table = Project.__table__
        live_subq = select(project_table.c.id)
        for model_cls in (
            DashboardCaseFirstRun,
            DashboardDailyRollup,
            DashboardCaseVersionLatest,
            DashboardVersionRollup,
        ):
            tbl = model_cls.__table__
            conn.execute(delete(tbl).where(~tbl.c.project_id.in_(live_subq)))
        return result

    return None


def register_rollup_listeners() -> None:
    """Register idempotent SQLAlchemy session hooks for rollup maintenance."""
    global _LISTENERS_REGISTERED
    if _LISTENERS_REGISTERED:
        return
    event.listen(Session, "after_flush", _on_after_flush)
    event.listen(Session, "after_soft_rollback", _on_after_soft_rollback)
    event.listen(Session, "do_orm_execute", _on_do_orm_execute)
    _LISTENERS_REGISTERED = True


register_rollup_listeners()
