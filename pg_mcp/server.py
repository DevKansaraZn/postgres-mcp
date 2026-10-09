"""Read-only Postgres MCP server.

Exposes schema introspection and SELECT-only query execution over MCP stdio.

Read-only is enforced in three independent layers:
  1. a static SQL guard (:mod:`pg_mcp.guard`) that only accepts single,
     non-locking SELECT/WITH/TABLE/VALUES/SHOW statements;
  2. a session started with ``default_transaction_read_only=on``;
  3. an explicit ``READ ONLY`` transaction that is always rolled back.

A fourth layer is recommended and documented in the README: point the server at
a dedicated Postgres role that only holds CONNECT/USAGE/SELECT.
"""

from __future__ import annotations

import sys

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .config import ConfigError, load_settings
from .db import Database, QueryError
from .guard import UnsafeQueryError, check_query
from .render import summarize, table

INSTRUCTIONS = """\
Read-only Postgres access. Every tool here is strictly SELECT-only: INSERT,
UPDATE, DELETE, MERGE, DDL, COPY, locking reads and session-state changes are
rejected before they reach the database, and the database connection itself is
read-only.

Suggested flow: `list_schemas` -> `list_tables` -> `describe_table` to learn the
shape of the data, then `run_query` for the actual question. Use `explain_query`
before running anything expensive against a large table.
"""

mcp = MCPServer(
    name="postgres-readonly",
    title="Postgres (read-only)",
    instructions=INSTRUCTIONS,
    version="1.0.0",
)

_READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)

try:
    SETTINGS = load_settings()
except ConfigError as exc:  # pragma: no cover - startup path
    print(f"[postgres-readonly] configuration error: {exc}", file=sys.stderr)
    raise SystemExit(2) from exc

DB = Database(SETTINGS)

# NOTE: no '%' anywhere in here -- this string is spliced into queries that are
# executed with parameters, and psycopg would read a stray '%' as a placeholder.
_SYSTEM_SCHEMA_FILTER = (
    "" if SETTINGS.allow_system_schemas
    else "AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
         "AND NOT starts_with(n.nspname, 'pg_toast') "
         "AND NOT starts_with(n.nspname, 'pg_temp') "
)


def _contains_pattern(text: str | None) -> str | None:
    """Build a safe ILIKE pattern, escaping the user's own wildcards."""
    if text is None:
        return None
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"

RELKIND_LABELS = {
    "r": "table",
    "p": "partitioned table",
    "v": "view",
    "m": "materialized view",
    "f": "foreign table",
    "t": "toast",
}


def _fail(exc: Exception) -> str:
    return f"ERROR: {exc}"


def _qualified(table_name: str, schema: str | None) -> str:
    name = table_name.strip()
    if schema and "." not in name:
        return f'"{schema}"."{name}"' if not name.startswith('"') else f'"{schema}".{name}'
    return name


# --------------------------------------------------------------------------- #
# Introspection
# --------------------------------------------------------------------------- #

@mcp.tool(
    description=(
        "List schemas in the database with their owner and table count. "
        "Start here when you do not know the database layout."
    ),
    annotations=_READ_ONLY,
)
def list_schemas() -> str:
    sql = f"""
        SELECT n.nspname AS schema,
               pg_get_userbyid(n.nspowner) AS owner,
               count(c.oid) FILTER (WHERE c.relkind IN ('r','p','v','m','f')) AS objects
        FROM pg_namespace n
        LEFT JOIN pg_class c ON c.relnamespace = n.oid
        WHERE true {_SYSTEM_SCHEMA_FILTER}
        GROUP BY 1, 2
        ORDER BY 1
    """
    try:
        return table(DB.run_internal(sql), max_cell_chars=SETTINGS.max_cell_chars)
    except QueryError as exc:
        return _fail(exc)


@mcp.tool(
    description=(
        "List tables, views and materialized views. Optionally filter by schema "
        "and by a case-insensitive substring of the table name. Shows estimated "
        "row counts and on-disk size."
    ),
    annotations=_READ_ONLY,
)
def list_tables(schema: str | None = None, name_contains: str | None = None, limit: int = 200) -> str:
    limit = max(1, min(int(limit), 2000))
    sql = f"""
        SELECT n.nspname AS schema,
               c.relname AS name,
               c.relkind AS kind,
               c.reltuples::bigint AS est_rows,
               pg_size_pretty(pg_total_relation_size(c.oid)) AS total_size,
               obj_description(c.oid, 'pg_class') AS comment
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r','p','v','m','f')
          {_SYSTEM_SCHEMA_FILTER}
          AND (%s::text IS NULL OR n.nspname = %s)
          AND (%s::text IS NULL OR c.relname ILIKE %s)
        ORDER BY pg_total_relation_size(c.oid) DESC, n.nspname, c.relname
        LIMIT %s
    """
    pattern = _contains_pattern(name_contains)
    try:
        result = DB.run_internal(
            sql, (schema, schema, name_contains, pattern, limit), max_rows=limit
        )
    except QueryError as exc:
        return _fail(exc)

    kind_idx = result.columns.index("kind")
    result.rows = [
        tuple(
            RELKIND_LABELS.get(v, v) if i == kind_idx else v
            for i, v in enumerate(row)
        )
        for row in result.rows
    ]
    rendered = table(result, max_cell_chars=SETTINGS.max_cell_chars)
    note = "\nNote: est_rows comes from the planner statistics and is approximate."
    return rendered + note


@mcp.tool(
    description=(
        "Show the full definition of one table or view: columns with types, "
        "nullability and defaults, plus constraints, indexes, size and comments. "
        "Accepts 'table', 'schema.table', or table plus the schema argument."
    ),
    annotations=_READ_ONLY,
)
def describe_table(table_name: str, schema: str | None = None) -> str:
    target = _qualified(table_name, schema)
    sections: list[str] = [f"## {target}"]

    try:
        meta = DB.run_internal(
            """
            SELECT n.nspname, c.relname, c.relkind,
                   c.reltuples::bigint,
                   pg_size_pretty(pg_total_relation_size(c.oid)),
                   obj_description(c.oid, 'pg_class')
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.oid = %s::regclass
            """,
            (target,),
        )
    except QueryError as exc:
        return _fail(exc)

    if not meta.rows:
        return f"ERROR: relation {target} not found."

    nsp, rel, relkind, est_rows, size, comment = meta.rows[0]
    sections[0] = f"## {nsp}.{rel} ({RELKIND_LABELS.get(relkind, relkind)})"
    sections.append(f"Estimated rows: {est_rows:,} | Total size: {size}")
    if comment:
        sections.append(f"Comment: {comment}")

    try:
        cols = DB.run_internal(
            """
            SELECT a.attname AS column,
                   format_type(a.atttypid, a.atttypmod) AS type,
                   CASE WHEN a.attnotnull THEN 'NOT NULL' ELSE '' END AS nullable,
                   pg_get_expr(d.adbin, d.adrelid) AS default,
                   col_description(a.attrelid, a.attnum) AS comment
            FROM pg_attribute a
            LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
            WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped
            ORDER BY a.attnum
            """,
            (target,),
        )
        sections.append("\n### Columns\n" + table(cols, max_cell_chars=SETTINGS.max_cell_chars))

        cons = DB.run_internal(
            """
            SELECT conname AS name, pg_get_constraintdef(oid) AS definition
            FROM pg_constraint
            WHERE conrelid = %s::regclass
            ORDER BY CASE contype WHEN 'p' THEN 0 WHEN 'u' THEN 1
                                  WHEN 'f' THEN 2 ELSE 3 END, conname
            """,
            (target,),
        )
        if cons.rows:
            sections.append(
                "\n### Constraints\n" + table(cons, max_cell_chars=SETTINGS.max_cell_chars)
            )

        idx = DB.run_internal(
            "SELECT indexname AS name, indexdef AS definition FROM pg_indexes "
            "WHERE schemaname = %s AND tablename = %s ORDER BY indexname",
            (nsp, rel),
        )
        if idx.rows:
            sections.append(
                "\n### Indexes\n" + table(idx, max_cell_chars=SETTINGS.max_cell_chars)
            )

        if relkind in ("v", "m"):
            view = DB.run_internal("SELECT pg_get_viewdef(%s::regclass, true)", (target,))
            if view.rows and view.rows[0][0]:
                sections.append("\n### Definition\n```sql\n" + str(view.rows[0][0]).strip() + "\n```")
    except QueryError as exc:
        return "\n".join(sections) + f"\n\n{_fail(exc)}"

    return "\n".join(sections)


@mcp.tool(
    description=(
        "Find columns by name across the database. Useful for locating which "
        "tables carry a field like 'customer_id' or 'created_at'."
    ),
    annotations=_READ_ONLY,
)
def search_columns(column_contains: str, schema: str | None = None, limit: int = 100) -> str:
    limit = max(1, min(int(limit), 1000))
    sql = f"""
        SELECT n.nspname AS schema,
               c.relname AS table,
               a.attname AS column,
               format_type(a.atttypid, a.atttypmod) AS type
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE a.attnum > 0 AND NOT a.attisdropped
          AND c.relkind IN ('r','p','v','m','f')
          {_SYSTEM_SCHEMA_FILTER}
          AND a.attname ILIKE %s
          AND (%s::text IS NULL OR n.nspname = %s)
        ORDER BY n.nspname, c.relname, a.attnum
        LIMIT %s
    """
    try:
        return table(
            DB.run_internal(
                sql, (_contains_pattern(column_contains), schema, schema, limit), max_rows=limit
            ),
            max_cell_chars=SETTINGS.max_cell_chars,
        )
    except QueryError as exc:
        return _fail(exc)


@mcp.tool(
    description="Return the first few rows of a table or view, to see what the data looks like.",
    annotations=_READ_ONLY,
)
def sample_rows(table_name: str, schema: str | None = None, limit: int = 10) -> str:
    limit = max(1, min(int(limit), SETTINGS.max_rows))
    target = _qualified(table_name, schema)
    try:
        # Resolve through regclass first so the identifier is validated by
        # Postgres and quoted correctly for the SELECT.
        resolved = DB.run_internal(
            "SELECT quote_ident(n.nspname) || '.' || quote_ident(c.relname) "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.oid = %s::regclass",
            (target,),
        )
        if not resolved.rows:
            return f"ERROR: relation {target} not found."
        safe_name = resolved.rows[0][0]
        result = DB.run_sql(f"SELECT * FROM {safe_name}", max_rows=limit)
    except (QueryError, UnsafeQueryError) as exc:
        return _fail(exc)

    rendered = table(result, max_cell_chars=SETTINGS.max_cell_chars)
    more = " (more rows exist)" if result.truncated else ""
    return f"{rendered}\n\nFirst {len(result.rows)} row(s) of {safe_name}{more}."


# --------------------------------------------------------------------------- #
# Query execution
# --------------------------------------------------------------------------- #

@mcp.tool(
    description=(
        "Run a read-only SQL query and return the rows. ONLY single SELECT "
        "statements are accepted (WITH/TABLE/VALUES/SHOW are also fine). Any "
        "INSERT, UPDATE, DELETE, MERGE, DDL, COPY, SELECT ... FOR UPDATE, SET, "
        "or multi-statement input is rejected before reaching the database, and "
        "the connection is read-only regardless. Results are capped; add your "
        "own LIMIT or raise max_rows as needed."
    ),
    annotations=_READ_ONLY,
)
def run_query(sql: str, max_rows: int | None = None, timeout_ms: int | None = None) -> str:
    rows_cap = SETTINGS.max_rows if max_rows is None else max(1, min(int(max_rows), 10_000))
    timeout = (
        None if timeout_ms is None
        else max(100, min(int(timeout_ms), SETTINGS.statement_timeout_ms))
    )
    try:
        checked = check_query(sql)
    except UnsafeQueryError as exc:
        return f"REJECTED (read-only policy): {exc}"
    try:
        result = DB.run_checked(checked, max_rows=rows_cap, statement_timeout_ms=timeout)
    except QueryError as exc:
        return _fail(exc)
    return summarize(result, max_cell_chars=SETTINGS.max_cell_chars, max_rows=rows_cap)


@mcp.tool(
    description=(
        "Show the Postgres query plan for a SELECT without returning its rows. "
        "Use this to check cost before running an expensive query. Set "
        "analyze=true to actually execute it and report real timings."
    ),
    annotations=_READ_ONLY,
)
def explain_query(sql: str, analyze: bool = False) -> str:
    try:
        checked = check_query(sql)
    except UnsafeQueryError as exc:
        return f"REJECTED (read-only policy): {exc}"

    options = "ANALYZE true, BUFFERS true, COSTS true, FORMAT TEXT" if analyze else "COSTS true, FORMAT TEXT"
    explain_sql = f"EXPLAIN ({options}) {checked.sql}"
    try:
        result = DB.run_internal(explain_sql, max_rows=500)
    except QueryError as exc:
        return _fail(exc)
    return "\n".join(str(row[0]) for row in result.rows)


@mcp.tool(
    description=(
        "Report the server version, current database/user/role, the configured "
        "limits, and confirm that the session is read-only."
    ),
    annotations=_READ_ONLY,
)
def database_info() -> str:
    try:
        result = DB.run_internal(
            """
            SELECT version() AS server_version,
                   current_database() AS database,
                   current_user AS "user",
                   current_setting('transaction_read_only') AS transaction_read_only,
                   current_setting('default_transaction_read_only') AS session_read_only,
                   current_setting('statement_timeout') AS statement_timeout,
                   pg_size_pretty(pg_database_size(current_database())) AS database_size
            """
        )
    except QueryError as exc:
        return _fail(exc)

    pairs = zip(result.columns, result.rows[0]) if result.rows else []
    lines = [f"- **{k}**: {v}" for k, v in pairs]
    lines.append(f"- **target**: {SETTINGS.describe_target()}")
    lines.append(f"- **max_rows default**: {SETTINGS.max_rows}")
    lines.append(f"- **max cell chars**: {SETTINGS.max_cell_chars}")
    lines.append("- **write access**: none — guard + read-only session + read-only transaction")
    return "\n".join(lines)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
