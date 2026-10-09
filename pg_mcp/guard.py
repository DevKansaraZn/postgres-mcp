"""Read-only SQL guard.

This is the *first* of three independent read-only layers (the other two are the
``default_transaction_read_only`` session setting and the ``READ ONLY``
transaction the query actually runs in). It statically rejects anything that is
not a single, plain, non-locking SELECT.

The tricky part is that keywords must not be matched inside string literals,
dollar-quoted bodies, quoted identifiers or comments -- otherwise
``SELECT * FROM t WHERE note = 'please delete'`` would be rejected, and worse,
``SELECT 'x' /* ; DROP ... */`` style smuggling would be judged on the wrong
text. So we first replace every such span with a neutral placeholder, then run
the keyword checks against that sanitized copy. The *original* SQL is what gets
executed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

IDENT_START = re.compile(r"[A-Za-z_]")
DOLLAR_TAG = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")

#: Statements a read-only client is allowed to begin with.
ALLOWED_LEADING_KEYWORDS = frozenset({"select", "with", "table", "values", "show"})

#: Whole-word tokens that must never appear anywhere in a read-only statement.
#: Each entry is matched case-insensitively with word boundaries, against the
#: sanitized SQL (so identifiers/strings named e.g. 'delete' are unaffected).
FORBIDDEN_WORDS: tuple[str, ...] = (
    "insert",
    "update",
    "delete",
    "merge",
    "upsert",
    "truncate",
    "drop",
    "create",
    "alter",
    "rename",
    "grant",
    "revoke",
    "reassign",
    "copy",
    "vacuum",
    "analyze",
    "analyse",
    "reindex",
    "refresh",
    "cluster",
    "lock",
    "unlock",
    "call",
    "do",
    "prepare",
    "deallocate",
    "execute",
    "discard",
    "listen",
    "unlisten",
    "notify",
    "load",
    "checkpoint",
    "begin",
    "start",
    "commit",
    "rollback",
    "savepoint",
    "abort",
    "import",
    "attach",
    "detach",
    "into",  # SELECT ... INTO creates a table
    "returning",
)

#: Multi-word / contextual constructs that must be blocked.
FORBIDDEN_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bfor\s+update\b", "SELECT ... FOR UPDATE takes row locks"),
    (r"\bfor\s+no\s+key\s+update\b", "SELECT ... FOR NO KEY UPDATE takes row locks"),
    (r"\bfor\s+share\b", "SELECT ... FOR SHARE takes row locks"),
    (r"\bfor\s+key\s+share\b", "SELECT ... FOR KEY SHARE takes row locks"),
    (r"\bcomment\s+on\b", "COMMENT ON modifies catalog metadata"),
    (r"\bsecurity\s+label\b", "SECURITY LABEL modifies catalog metadata"),
    (r"\bset\s+(session|local|role|constraints|transaction)\b", "SET changes session state"),
    (r"\bset\s+[a-z_][a-z0-9_.]*\s*(=|\bto\b)", "SET changes session state"),
    (r"\breset\s+(all|[a-z_][a-z0-9_.]*)\b", "RESET changes session state"),
)

#: Functions that read/write the server filesystem, reach other hosts, take
#: locks, change settings, or let a caller stall the backend.
FORBIDDEN_FUNCTIONS: tuple[str, ...] = (
    r"pg_read_file",
    r"pg_read_binary_file",
    r"pg_ls_\w+",
    r"pg_stat_file",
    r"pg_file_\w+",
    r"lo_import",
    r"lo_export",
    r"lo_put",
    r"lo_unlink",
    r"lo_from_bytea",
    r"dblink\w*",
    r"pg_sleep\w*",
    r"pg_terminate_backend",
    r"pg_cancel_backend",
    r"pg_reload_conf",
    r"pg_rotate_logfile",
    r"pg_promote",
    r"pg_switch_wal",
    r"pg_create_restore_point",
    r"pg_\w*replication_slot\w*",
    r"pg_replication_origin_\w+",
    r"pg_logical_emit_message",
    r"pg_wal_replay_\w+",
    r"pg_backup_\w+",
    r"pg_stat_reset\w*",
    r"pg_advisory_\w*lock\w*",
    r"pg_try_advisory_\w*lock\w*",
    r"pg_advisory_unlock\w*",
    r"set_config",
    r"pg_import_system_collations",
    r"pg_drop_\w+",
    r"pg_tablespace_\w*size",  # harmless, but keeps the pg_drop/pg_* surface tight
)

_FORBIDDEN_WORD_RE = re.compile(
    r"\b(" + "|".join(FORBIDDEN_WORDS) + r")\b", re.IGNORECASE
)
_FORBIDDEN_FUNC_RE = re.compile(
    r"\b(" + "|".join(FORBIDDEN_FUNCTIONS) + r")\s*\(", re.IGNORECASE
)
_COMPILED_PATTERNS = tuple(
    (re.compile(pattern, re.IGNORECASE), reason) for pattern, reason in FORBIDDEN_PATTERNS
)


class UnsafeQueryError(ValueError):
    """Raised when a statement is not a safe, single, read-only SELECT."""


@dataclass(frozen=True)
class CheckedQuery:
    """A statement that passed the guard."""

    sql: str  #: original text, with any trailing semicolon removed
    sanitized: str  #: literals/comments/identifiers blanked out
    leading_keyword: str
    is_cursorable: bool  #: can be run through a server-side cursor


def sanitize(sql: str) -> str:
    """Replace literals, dollar-quoted bodies, quoted identifiers and comments.

    The returned string has the same length as the input so error offsets stay
    meaningful, and contains only structural SQL plus neutral placeholders.
    """
    out: list[str] = []
    i = 0
    n = len(sql)

    while i < n:
        ch = sql[i]

        # ---- line comment -------------------------------------------------
        if ch == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j == -1 else j
            out.append(" " * (j - i))
            i = j
            continue

        # ---- block comment (nestable in Postgres) -------------------------
        if ch == "/" and sql.startswith("/*", i):
            depth = 1
            j = i + 2
            while j < n and depth:
                if sql.startswith("/*", j):
                    depth += 1
                    j += 2
                elif sql.startswith("*/", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            out.append(" " * (j - i))
            i = j
            continue

        # ---- dollar-quoted string ----------------------------------------
        if ch == "$":
            m = DOLLAR_TAG.match(sql, i)
            if m:
                tag = m.group(0)
                end = sql.find(tag, m.end())
                j = n if end == -1 else end + len(tag)
                out.append("'" + " " * max(0, j - i - 2) + "'" if j - i >= 2 else " " * (j - i))
                i = j
                continue

        # ---- single-quoted string ----------------------------------------
        if ch == "'":
            # Backslash escapes only apply to E'' strings; with
            # standard_conforming_strings=on (the default) a lone backslash is
            # literal, so treating it as an escape would let a crafted literal
            # swallow the rest of the statement.
            escaped = i > 0 and sql[i - 1] in "Ee" and not _is_ident_char(sql, i - 2)
            j = i + 1
            while j < n:
                if escaped and sql[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append("'" + " " * max(0, j - i - 2) + "'" if j - i >= 2 else " " * (j - i))
            i = j
            continue

        # ---- double-quoted identifier ------------------------------------
        if ch == '"':
            j = i + 1
            while j < n:
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            # Replace the body with 'q' so it still reads as an identifier but
            # carries no keywords.
            inner = max(0, j - i - 2)
            out.append('"' + "q" * inner + '"' if j - i >= 2 else " " * (j - i))
            i = j
            continue

        out.append(ch)
        i += 1

    sanitized = "".join(out)
    # Length is preserved by every branch above; assert cheaply in debug use.
    return sanitized


def _is_ident_char(sql: str, idx: int) -> bool:
    return 0 <= idx < len(sql) and (sql[idx].isalnum() or sql[idx] == "_")


def check_query(sql: str, *, allow_explain: bool = False) -> CheckedQuery:
    """Validate ``sql`` as a single read-only statement.

    Raises :class:`UnsafeQueryError` with a specific reason on rejection.
    """
    if sql is None or not sql.strip():
        raise UnsafeQueryError("Empty query.")

    if "\x00" in sql:
        raise UnsafeQueryError("Query contains a null byte.")

    sanitized = sanitize(sql)

    # Trailing semicolons (and trailing whitespace/comments) are fine; anything
    # after them is a second statement.
    stripped = sanitized.rstrip()
    trailing = 0
    while stripped.endswith(";"):
        stripped = stripped[:-1].rstrip()
        trailing += 1

    if ";" in stripped:
        raise UnsafeQueryError(
            "Multiple statements are not allowed. Send exactly one SELECT."
        )
    if trailing > 1:
        raise UnsafeQueryError("Multiple statements are not allowed.")

    if not stripped.strip():
        raise UnsafeQueryError("Query contains no statement.")

    # Keep the executable text aligned with what we validated.
    original = sql.rstrip()
    while original.endswith(";"):
        original = original[:-1].rstrip()

    core = stripped.strip()
    match = re.match(r"\(*\s*([A-Za-z_][A-Za-z0-9_]*)", core)
    if not match:
        raise UnsafeQueryError("Query does not start with a SQL keyword.")
    leading = match.group(1).lower()

    allowed = set(ALLOWED_LEADING_KEYWORDS)
    if allow_explain:
        allowed.add("explain")
    if leading not in allowed:
        raise UnsafeQueryError(
            f"Only read-only statements are permitted; this one starts with "
            f"{leading.upper()!r}. Allowed: {', '.join(sorted(k.upper() for k in allowed))}."
        )

    scan = core
    if allow_explain and leading == "explain":
        # Validate the inner statement too, but let ANALYZE/BUFFERS through in
        # the EXPLAIN option list.
        scan = re.sub(r"^\s*explain\s*(\([^)]*\))?", " ", core, count=1, flags=re.IGNORECASE)

    word = _FORBIDDEN_WORD_RE.search(scan)
    if word:
        raise UnsafeQueryError(
            f"Blocked keyword {word.group(1).upper()!r}: this server only runs SELECT queries."
        )

    for pattern, reason in _COMPILED_PATTERNS:
        if pattern.search(scan):
            raise UnsafeQueryError(f"Blocked construct: {reason}.")

    func = _FORBIDDEN_FUNC_RE.search(scan)
    if func:
        raise UnsafeQueryError(
            f"Blocked function {func.group(1)}(): filesystem, replication, locking, "
            "settings and sleep functions are not available."
        )

    return CheckedQuery(
        sql=original,
        sanitized=sanitized,
        leading_keyword=leading,
        is_cursorable=leading in {"select", "with", "table", "values"},
    )
