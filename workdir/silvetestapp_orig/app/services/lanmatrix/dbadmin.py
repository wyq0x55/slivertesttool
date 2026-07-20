"""Admin-only PostgreSQL introspection + SQL console.

This module powers the system-administrator "database management" page. It is
strictly PostgreSQL-oriented and reuses the application's SQLAlchemy engine
(``app.extensions.db``) so it shares the same connection pool and credentials
configured through ``.env`` / :class:`app.config.Config`.

Safety model (the page is reachable only by ``is_system_admin`` users):

* **Read-only mode (default)** rejects any statement whose leading keyword is
  not query-like and, as a belt-and-braces measure, runs it inside a
  transaction that is always rolled back — so nothing can be written even if a
  write slips through.
* **Write mode** must be explicitly requested; the statement runs in its own
  transaction and is committed on success.

Result sets are capped at :data:`MAX_ROWS` rows and values are coerced to
JSON-safe representations.
"""

from __future__ import annotations

import datetime as _dt
import decimal
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from ...extensions import db

MAX_ROWS = 500

# Leading keywords considered "read-only" for the guarded console mode.
_READ_KEYWORDS = {
    "select", "with", "show", "explain", "table", "values",
}


class DbAdminError(Exception):
    """Raised for SQL/introspection failures surfaced to the admin UI."""


def _leading_keyword(sql: str) -> str:
    """Return the first SQL keyword, skipping leading comments/whitespace."""
    s = sql.strip()
    # Strip leading line (``--``) and block (``/* */``) comments.
    changed = True
    while changed:
        changed = False
        if s.startswith("--"):
            nl = s.find("\n")
            s = "" if nl < 0 else s[nl + 1:]
            s = s.lstrip()
            changed = True
        elif s.startswith("/*"):
            end = s.find("*/")
            s = "" if end < 0 else s[end + 2:]
            s = s.lstrip()
            changed = True
    token = ""
    for ch in s:
        if ch.isalpha():
            token += ch
        else:
            break
    return token.lower()


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, decimal.Decimal):
        # Keep integers exact; represent fractionals as float for the grid.
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"\\x{bytes(value).hex()}"
    return str(value)


def _scalar(conn, sql: str, default: Any = None) -> Any:
    try:
        return conn.execute(text(sql)).scalar()
    except SQLAlchemyError:
        return default


def overview() -> dict:
    """Return connection metadata, database size and per-table statistics."""
    dialect = db.engine.dialect.name
    info: dict[str, Any] = {"backend": dialect}
    if dialect != "postgresql":
        # The platform targets PostgreSQL; degrade gracefully for other engines.
        info["error"] = f"当前后端为 {dialect}，本页面仅完整支持 PostgreSQL。"
        info["tables"] = []
        return info

    try:
        with db.engine.connect() as conn:
            info["version"] = _scalar(conn, "SELECT version()")
            info["database"] = _scalar(conn, "SELECT current_database()")
            info["db_user"] = _scalar(conn, "SELECT current_user")
            info["server_addr"] = _jsonable(
                _scalar(conn, "SELECT host(inet_server_addr())"))
            info["server_port"] = _jsonable(
                _scalar(conn, "SELECT inet_server_port()"))
            info["size_bytes"] = _scalar(
                conn, "SELECT pg_database_size(current_database())")
            info["size_pretty"] = _scalar(
                conn, "SELECT pg_size_pretty(pg_database_size(current_database()))")
            info["now"] = _jsonable(_scalar(conn, "SELECT now()"))

            rows = conn.execute(text(
                """
                SELECT n.nspname AS schema,
                       c.relname AS name,
                       c.reltuples::bigint AS est_rows,
                       pg_total_relation_size(c.oid) AS total_bytes,
                       pg_size_pretty(pg_total_relation_size(c.oid)) AS size_pretty
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind = 'r'
                  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
                ORDER BY pg_total_relation_size(c.oid) DESC
                """
            )).mappings().all()
            info["tables"] = [
                {
                    "schema": r["schema"],
                    "name": r["name"],
                    "est_rows": int(r["est_rows"] or 0),
                    "total_bytes": int(r["total_bytes"] or 0),
                    "size_pretty": r["size_pretty"],
                }
                for r in rows
            ]
            info["table_count"] = len(info["tables"])
    except SQLAlchemyError as ex:
        raise DbAdminError(f"读取数据库信息失败：{ex}") from ex
    return info


def run_sql(sql: str, *, read_only: bool = True,
            max_rows: int = MAX_ROWS) -> dict:
    """Execute a single SQL statement and return a JSON-safe result payload."""
    sql = (sql or "").strip().rstrip(";").strip()
    if not sql:
        raise DbAdminError("请输入 SQL 语句")
    keyword = _leading_keyword(sql)
    if read_only and keyword not in _READ_KEYWORDS:
        raise DbAdminError(
            "只读模式仅允许查询类语句（SELECT/WITH/SHOW/EXPLAIN/TABLE/VALUES）。"
            "如需执行写操作，请关闭“只读”开关。")

    started = time.perf_counter()
    result_payload: dict[str, Any] = {
        "read_only": read_only,
        "command": keyword.upper(),
        "columns": [],
        "rows": [],
        "rowcount": 0,
        "returns_rows": False,
        "truncated": False,
    }
    try:
        with db.engine.connect() as conn:
            trans = conn.begin()
            try:
                result = conn.execute(text(sql))
                if result.returns_rows:
                    columns = list(result.keys())
                    fetched = result.fetchmany(max_rows + 1)
                    truncated = len(fetched) > max_rows
                    data_rows = fetched[:max_rows]
                    result_payload.update(
                        returns_rows=True,
                        columns=columns,
                        rows=[[_jsonable(v) for v in row] for row in data_rows],
                        rowcount=len(data_rows),
                        truncated=truncated,
                    )
                else:
                    result_payload["rowcount"] = (
                        result.rowcount if result.rowcount is not None else 0)
                # Read-only always rolls back; write mode commits.
                if read_only:
                    trans.rollback()
                else:
                    trans.commit()
            except Exception:
                trans.rollback()
                raise
    except SQLAlchemyError as ex:
        # Surface the concise DB message (SQLAlchemy wraps it).
        msg = getattr(getattr(ex, "orig", None), "args", None)
        detail = msg[0] if msg else str(ex)
        raise DbAdminError(str(detail).strip()) from ex

    result_payload["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return result_payload


# --------------------------------------------------------------------------- #
# No-SQL table CRUD (system_admin only)
#
# A form-driven browse/insert/update/delete layer over the physical tables so
# admins do not have to hand-write SQL. Every table and column identifier is
# validated against live catalog introspection before it is interpolated, and
# all row values are passed as bound parameters — so this stays injection-safe
# despite composing dynamic statements.
# --------------------------------------------------------------------------- #
def _require_pg() -> None:
    if db.engine.dialect.name != "postgresql":
        raise DbAdminError("表管理功能仅支持 PostgreSQL 后端。")


def _quote_ident(name: str) -> str:
    """Safely double-quote a PostgreSQL identifier (already whitelist-checked)."""
    return '"' + str(name).replace('"', '""') + '"'


def list_manageable_tables() -> list:
    """Return user tables in the ``public`` schema available for CRUD."""
    _require_pg()
    with db.engine.connect() as conn:
        rows = conn.execute(text(
            """
            SELECT c.relname AS name,
                   c.reltuples::bigint AS est_rows
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'r' AND n.nspname = 'public'
            ORDER BY c.relname
            """
        )).mappings().all()
    return [{"name": r["name"], "est_rows": int(r["est_rows"] or 0)} for r in rows]


def _table_names() -> set:
    return {t["name"] for t in list_manageable_tables()}


def _assert_table(table: str) -> str:
    if table not in _table_names():
        raise DbAdminError(f"表不存在或不可管理：{table}")
    return table


def table_schema(table: str) -> dict:
    """Introspect a table: columns (type/nullable/default) + primary key."""
    _require_pg()
    _assert_table(table)
    with db.engine.connect() as conn:
        cols = conn.execute(text(
            """
            SELECT column_name, data_type, udt_name, is_nullable,
                   column_default, character_maximum_length,
                   is_identity, ordinal_position
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = :t
            ORDER BY ordinal_position
            """
        ), {"t": table}).mappings().all()
        pk = conn.execute(text(
            """
            SELECT a.attname AS column_name
            FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid
                                AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = ('public.' || quote_ident(:t))::regclass
              AND i.indisprimary
            """
        ), {"t": table}).mappings().all()
    pk_cols = [r["column_name"] for r in pk]
    columns = []
    for c in cols:
        default = c["column_default"]
        columns.append({
            "name": c["column_name"],
            "data_type": c["data_type"],
            "udt_name": c["udt_name"],
            "nullable": c["is_nullable"] == "YES",
            "default": default,
            "max_length": c["character_maximum_length"],
            "is_identity": c["is_identity"] == "YES",
            # Serial/identity or default-bearing columns are optional on insert.
            "auto": c["is_identity"] == "YES" or default is not None,
            "is_pk": c["column_name"] in pk_cols,
        })
    return {"table": table, "columns": columns, "primary_key": pk_cols}


def _column_map(table: str) -> dict:
    return {c["name"]: c for c in table_schema(table)["columns"]}


def _coerce_in(value: Any, col: dict) -> Any:
    """Turn a JSON value from the form into a bind-ready Python value."""
    if value is None:
        return None
    if isinstance(value, str):
        s = value
        udt = (col.get("udt_name") or "").lower()
        data_type = (col.get("data_type") or "").lower()
        texty = udt in ("text", "varchar", "bpchar", "name", "citext") or \
            data_type in ("text", "character varying", "character")
        # Empty string on a non-text column means SQL NULL / default.
        if s == "" and not texty:
            return None
        if udt == "bool":
            low = s.strip().lower()
            if low in ("true", "t", "1", "yes", "y", "是"):
                return True
            if low in ("false", "f", "0", "no", "n", "否"):
                return False
        return s
    return value


def read_rows(table: str, *, page: int = 1, page_size: int = 50,
              order_by=None, descending: bool = False) -> dict:
    """Paginated browse of a table's rows with total count."""
    _require_pg()
    _assert_table(table)
    schema = table_schema(table)
    colnames = [c["name"] for c in schema["columns"]]
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), MAX_ROWS))
    qtable = _quote_ident(table)

    order_sql = ""
    if order_by:
        if order_by not in colnames:
            raise DbAdminError(f"排序列不存在：{order_by}")
        direction = "DESC" if descending else "ASC"
        order_sql = f" ORDER BY {_quote_ident(order_by)} {direction}"
    elif schema["primary_key"]:
        order_sql = " ORDER BY " + ", ".join(
            _quote_ident(c) for c in schema["primary_key"])

    offset = (page - 1) * page_size
    with db.engine.connect() as conn:
        total = conn.execute(text(f"SELECT count(*) FROM {qtable}")).scalar() or 0
        result = conn.execute(
            text(f"SELECT * FROM {qtable}{order_sql} LIMIT :lim OFFSET :off"),
            {"lim": page_size, "off": offset})
        columns = list(result.keys())
        rows = [[_jsonable(v) for v in row] for row in result.fetchall()]
    return {
        "table": table,
        "columns": columns,
        "primary_key": schema["primary_key"],
        "rows": rows,
        "total": int(total),
        "page": page,
        "page_size": page_size,
        "pages": max(1, (int(total) + page_size - 1) // page_size),
    }


def _pk_where(schema: dict, pk_values: dict, params: dict) -> str:
    pk = schema["primary_key"]
    if not pk:
        raise DbAdminError("该表没有主键，无法安全定位单行（请使用 SQL 控制台）。")
    clauses = []
    for col in pk:
        if col not in (pk_values or {}):
            raise DbAdminError(f"缺少主键值：{col}")
        key = f"pk_{col}"
        params[key] = pk_values[col]
        clauses.append(f"{_quote_ident(col)} = :{key}")
    return " AND ".join(clauses)


def insert_row(table: str, values: dict) -> dict:
    """Insert a row from a dict of column→value; returns the created row."""
    _require_pg()
    _assert_table(table)
    colmap = _column_map(table)
    params: dict = {}
    cols: list = []
    placeholders: list = []
    for name, raw in (values or {}).items():
        if name not in colmap:
            continue  # silently drop unknown columns
        params[name] = _coerce_in(raw, colmap[name])
        cols.append(_quote_ident(name))
        placeholders.append(f":{name}")
    if not cols:
        raise DbAdminError("没有可写入的字段。")
    qtable = _quote_ident(table)
    sql = (f"INSERT INTO {qtable} ({', '.join(cols)}) "
           f"VALUES ({', '.join(placeholders)}) RETURNING *")
    return _exec_returning(sql, params)


def update_row(table: str, pk_values: dict, changes: dict) -> dict:
    """Update a single row identified by its primary key; returns the new row."""
    _require_pg()
    _assert_table(table)
    schema = table_schema(table)
    colmap = {c["name"]: c for c in schema["columns"]}
    params: dict = {}
    sets: list = []
    for name, raw in (changes or {}).items():
        if name not in colmap:
            continue
        key = f"set_{name}"
        params[key] = _coerce_in(raw, colmap[name])
        sets.append(f"{_quote_ident(name)} = :{key}")
    if not sets:
        raise DbAdminError("没有需要更新的字段。")
    where = _pk_where(schema, pk_values, params)
    qtable = _quote_ident(table)
    sql = f"UPDATE {qtable} SET {', '.join(sets)} WHERE {where} RETURNING *"
    row = _exec_returning(sql, params)
    if row is None:
        raise DbAdminError("未找到匹配的行（可能已被删除）。")
    return row


def delete_row(table: str, pk_values: dict) -> int:
    """Delete a single row identified by its primary key; returns rows deleted."""
    _require_pg()
    _assert_table(table)
    schema = table_schema(table)
    params: dict = {}
    where = _pk_where(schema, pk_values, params)
    qtable = _quote_ident(table)
    try:
        with db.engine.begin() as conn:
            result = conn.execute(
                text(f"DELETE FROM {qtable} WHERE {where}"), params)
            return result.rowcount or 0
    except SQLAlchemyError as ex:
        raise DbAdminError(_db_message(ex)) from ex


def _exec_returning(sql: str, params: dict):
    try:
        with db.engine.begin() as conn:
            result = conn.execute(text(sql), params)
            if not result.returns_rows:
                return None
            row = result.mappings().first()
            return {k: _jsonable(v) for k, v in dict(row).items()} if row else None
    except SQLAlchemyError as ex:
        raise DbAdminError(_db_message(ex)) from ex


def _db_message(ex) -> str:
    msg = getattr(getattr(ex, "orig", None), "args", None)
    detail = msg[0] if msg else str(ex)
    return str(detail).strip()
