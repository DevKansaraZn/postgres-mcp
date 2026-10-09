"""Configuration loading for the read-only Postgres MCP server.

Resolution order for connection settings:
  1. ``DATABASE_URL`` / ``PG_MCP_DATABASE_URL`` (a full libpq DSN)
  2. ``POSTGRES_*`` variables (as used by the official Postgres Docker image)
  3. Standard ``PG*`` variables (PGHOST, PGPORT, ...)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the package root (postgres-mcp/.env) without clobbering
# anything the MCP client already put in the environment.
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PACKAGE_ROOT / ".env", override=False)


class ConfigError(RuntimeError):
    """Raised when the server is not configured well enough to connect."""


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip() != "":
            return value.strip()
    return None


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    return max(minimum, min(maximum, value))


@dataclass(frozen=True)
class Settings:
    # Connection
    dsn: str | None
    host: str
    port: int
    user: str
    password: str
    dbname: str
    sslmode: str | None
    connect_timeout: int
    application_name: str

    # Safety / limits
    statement_timeout_ms: int
    idle_tx_timeout_ms: int
    lock_timeout_ms: int
    max_rows: int
    max_cell_chars: int
    allow_system_schemas: bool

    def describe_target(self) -> str:
        """Human-readable target, never including the password."""
        if self.dsn:
            return _redact_dsn(self.dsn)
        return f"{self.user}@{self.host}:{self.port}/{self.dbname}"

    def conninfo_kwargs(self) -> dict[str, object]:
        """Keyword arguments for ``psycopg.connect``."""
        options = " ".join(
            [
                "-c default_transaction_read_only=on",
                "-c transaction_read_only=on",
                f"-c statement_timeout={self.statement_timeout_ms}",
                f"-c idle_in_transaction_session_timeout={self.idle_tx_timeout_ms}",
                f"-c lock_timeout={self.lock_timeout_ms}",
            ]
        )
        kwargs: dict[str, object] = {
            "options": options,
            "connect_timeout": self.connect_timeout,
            "application_name": self.application_name,
            "autocommit": False,
        }
        if self.dsn:
            kwargs["conninfo"] = self.dsn
        else:
            kwargs.update(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                dbname=self.dbname,
            )
            if self.sslmode:
                kwargs["sslmode"] = self.sslmode
        return kwargs


def _redact_dsn(dsn: str) -> str:
    """Strip the password out of a DSN so it is safe to log or return."""
    import re

    redacted = re.sub(r"(?i)(password=)[^\s]+", r"\1***", dsn)
    redacted = re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", redacted)
    return redacted


def load_settings() -> Settings:
    dsn = _first_env("PG_MCP_DATABASE_URL", "DATABASE_URL")

    host = _first_env("PG_MCP_HOST", "POSTGRES_HOST", "PGHOST") or "localhost"
    port_raw = _first_env("PG_MCP_PORT", "POSTGRES_PORT", "PGPORT") or "5432"
    user = _first_env("PG_MCP_USER", "POSTGRES_USER", "PGUSER") or ""
    password = _first_env("PG_MCP_PASSWORD", "POSTGRES_PASSWORD", "PGPASSWORD") or ""
    dbname = _first_env("PG_MCP_DBNAME", "POSTGRES_DB", "PGDATABASE") or ""
    sslmode = _first_env("PG_MCP_SSLMODE", "PGSSLMODE")

    try:
        port = int(port_raw)
    except ValueError as exc:
        raise ConfigError(f"Port must be an integer, got {port_raw!r}") from exc

    if not dsn and (not user or not dbname):
        raise ConfigError(
            "Missing database configuration. Set DATABASE_URL, or set "
            "POSTGRES_USER and POSTGRES_DB (plus host/port/password) "
            "in the server's .env file. See .env.example."
        )

    return Settings(
        dsn=dsn,
        host=host,
        port=port,
        user=user,
        password=password,
        dbname=dbname,
        sslmode=sslmode,
        connect_timeout=_int_env("PG_MCP_CONNECT_TIMEOUT", 10, 1, 120),
        application_name=_first_env("PG_MCP_APP_NAME") or "claude-postgres-mcp-ro",
        statement_timeout_ms=_int_env("PG_MCP_STATEMENT_TIMEOUT_MS", 30_000, 100, 600_000),
        idle_tx_timeout_ms=_int_env("PG_MCP_IDLE_TX_TIMEOUT_MS", 60_000, 1_000, 600_000),
        lock_timeout_ms=_int_env("PG_MCP_LOCK_TIMEOUT_MS", 5_000, 100, 60_000),
        max_rows=_int_env("PG_MCP_MAX_ROWS", 200, 1, 10_000),
        max_cell_chars=_int_env("PG_MCP_MAX_CELL_CHARS", 500, 50, 20_000),
        allow_system_schemas=(
            (_first_env("PG_MCP_ALLOW_SYSTEM_SCHEMAS") or "false").lower()
            in {"1", "true", "yes", "on"}
        ),
    )
