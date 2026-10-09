"""Connection handling and query execution.

Every statement runs inside an explicit ``READ ONLY`` transaction that is always
rolled back, on a session where ``default_transaction_read_only`` is on. Even if
the SQL guard were bypassed, Postgres itself refuses the write.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg import sql as pgsql

from .config import Settings
from .guard import CheckedQuery, check_query


class QueryError(RuntimeError):
    """A database-side failure, already formatted for the caller."""


@dataclass
class QueryResult:
    columns: list[str] = field(default_factory=list)
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    truncated: bool = False
    duration_ms: float = 0.0
    notices: list[str] = field(default_factory=list)


class Database:
    """A lazily-opened, self-healing, read-only Postgres connection."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._conn: psycopg.Connection[Any] | None = None
        self._lock = threading.Lock()
        self._cursor_seq = 0

    # ------------------------------------------------------------------ conn
    def _connect(self) -> psycopg.Connection[Any]:
        kwargs = self.settings.conninfo_kwargs()
        conninfo = kwargs.pop("conninfo", "")
        try:
            conn = psycopg.connect(conninfo, **kwargs)  # type: ignore[arg-type]
        except psycopg.Error as exc:
            raise QueryError(
                f"Could not connect to {self.settings.describe_target()}: {_clean(exc)}"
            ) from exc
        # Belt and braces: psycopg also emits SET default_transaction_read_only.
        conn.read_only = True
        return conn

    def _healthy_conn(self) -> psycopg.Connection[Any]:
        if self._conn is not None and not self._conn.closed:
            try:
                self._conn.rollback()
                with self._conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
                self._conn.rollback()
                return self._conn
            except psycopg.Error:
                try:
                    self._conn.close()
                except Exception:  # pragma: no cover - best effort
                    pass
                self._conn = None
        self._conn = self._connect()
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None and not self._conn.closed:
                try:
                    self._conn.close()
                except Exception:  # pragma: no cover
                    pass
            self._conn = None

    # ------------------------------------------------------------- execution
    def _seal_transaction(
        self, conn: psycopg.Connection[Any], statement_timeout_ms: int | None
    ) -> None:
        """Make the current transaction irreversibly read-only, then pin limits.

        Postgres allows ``SET TRANSACTION READ WRITE`` at the top level of a
        transaction, but only *before any query has taken a snapshot*. Issuing a
        trivial SELECT here takes that snapshot, so from this point on the
        transaction cannot be promoted to read-write -- the attempt fails with
        "transaction read-write mode must be set before any query". That closes
        the one gap left by relying on the session setting alone.
        """
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
            timeout = (
                self.settings.statement_timeout_ms
                if statement_timeout_ms is None
                else int(statement_timeout_ms)
            )
            cur.execute(
                pgsql.SQL("SET LOCAL statement_timeout = {}").format(pgsql.Literal(timeout))
            )

    def run_checked(
        self,
        checked: CheckedQuery,
        *,
        max_rows: int,
        statement_timeout_ms: int | None = None,
    ) -> QueryResult:
        """Execute an already-validated statement and fetch at most ``max_rows``."""
        with self._lock:
            conn = self._healthy_conn()
            started = time.perf_counter()
            try:
                self._seal_transaction(conn, statement_timeout_ms)

                if checked.is_cursorable:
                    self._cursor_seq += 1
                    name = f"pg_mcp_ro_{self._cursor_seq}"
                    with conn.cursor(name=name) as cur:
                        cur.execute(checked.sql)  # type: ignore[arg-type]
                        rows = cur.fetchmany(max_rows + 1)
                        columns = [d.name for d in (cur.description or [])]
                else:
                    with conn.cursor() as cur:
                        cur.execute(checked.sql)  # type: ignore[arg-type]
                        columns = [d.name for d in (cur.description or [])]
                        rows = cur.fetchmany(max_rows + 1) if cur.description else []

                truncated = len(rows) > max_rows
                if truncated:
                    rows = rows[:max_rows]

                notices = [str(n).strip() for n in conn.notices][-5:] if hasattr(conn, "notices") else []
                return QueryResult(
                    columns=columns,
                    rows=list(rows),
                    truncated=truncated,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    notices=notices,
                )
            except psycopg.Error as exc:
                raise QueryError(_clean(exc)) from exc
            finally:
                try:
                    conn.rollback()
                except psycopg.Error:
                    self.close()

    def run_sql(self, sql: str, *, max_rows: int, statement_timeout_ms: int | None = None) -> QueryResult:
        """Validate then execute ``sql``."""
        return self.run_checked(
            check_query(sql),
            max_rows=max_rows,
            statement_timeout_ms=statement_timeout_ms,
        )

    def run_internal(
        self, sql: str, params: tuple[Any, ...] | None = None, *, max_rows: int = 2000
    ) -> QueryResult:
        """Run a server-authored catalog query (never user SQL) with parameters."""
        with self._lock:
            conn = self._healthy_conn()
            started = time.perf_counter()
            try:
                self._seal_transaction(conn, None)
                with conn.cursor() as cur:
                    cur.execute(sql, params)  # type: ignore[arg-type]
                    columns = [d.name for d in (cur.description or [])]
                    rows = cur.fetchmany(max_rows + 1) if cur.description else []
                truncated = len(rows) > max_rows
                return QueryResult(
                    columns=columns,
                    rows=list(rows[:max_rows]),
                    truncated=truncated,
                    duration_ms=(time.perf_counter() - started) * 1000,
                )
            except psycopg.Error as exc:
                raise QueryError(_clean(exc)) from exc
            finally:
                try:
                    conn.rollback()
                except psycopg.Error:
                    self.close()


def _clean(exc: BaseException) -> str:
    """Turn a psycopg error into a single readable line."""
    text = str(exc).strip()
    diag = getattr(exc, "diag", None)
    hint = getattr(diag, "hint", None) if diag else None
    parts = [" ".join(text.split("\n")[:3])]
    if hint:
        parts.append(f"Hint: {hint}")
    return " | ".join(p for p in parts if p)
