#!/usr/bin/env python
"""End-to-end proof that this server cannot write.

Layer 1 -- the SQL guard rejects write statements before they are sent.
Layer 2 -- even when the guard is deliberately bypassed, the Postgres session
           and transaction are READ ONLY, so the server itself refuses.

Nothing is created or modified: every probe is expected to fail. Run with:

    .venv/bin/python verify_readonly.py
"""

from __future__ import annotations

import sys

from pg_mcp.db import Database, QueryError
from pg_mcp.config import load_settings
from pg_mcp.guard import UnsafeQueryError, check_query

GUARD_PROBES = [
    "INSERT INTO pg_mcp_probe VALUES (1)",
    "UPDATE pg_mcp_probe SET a = 1",
    "DELETE FROM pg_mcp_probe",
    "DROP TABLE pg_mcp_probe",
    "CREATE TABLE pg_mcp_probe (a int)",
    "TRUNCATE pg_mcp_probe",
    "SELECT 1; DROP TABLE pg_mcp_probe",
    "SELECT * FROM pg_class FOR UPDATE",
    "SET default_transaction_read_only = off",
    "SELECT pg_read_file('/etc/passwd')",
]

# Sent straight to Postgres, skipping the guard entirely. Includes the two
# privilege-escalation routes an attacker would reach for.
DB_PROBES = [
    "CREATE TABLE pg_mcp_write_probe (a int)",
    "CREATE TEMP TABLE pg_mcp_write_probe_tmp (a int)",
    "SET TRANSACTION READ WRITE",
]

# Escalation attempts: these statements may well *succeed* (they only change a
# session default), so what matters is that a write afterwards still fails.
ESCALATIONS = [
    "SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE",
    "SET default_transaction_read_only = off",
]


def main() -> int:
    settings = load_settings()
    db = Database(settings)
    failures: list[str] = []

    print(f"Target: {settings.describe_target()}\n")
    print("Layer 1 - SQL guard")
    for sql in GUARD_PROBES:
        try:
            check_query(sql)
        except UnsafeQueryError as exc:
            print(f"  BLOCKED  {sql[:52]:<52} ({str(exc)[:46]})")
            continue
        failures.append(f"guard ALLOWED: {sql!r}")
        print(f"  ALLOWED  {sql}  <-- FAILURE")

    print("\nLayer 2 - Postgres read-only session (guard bypassed on purpose)")
    for sql in DB_PROBES:
        try:
            db.run_internal(sql)
        except QueryError as exc:
            print(f"  BLOCKED  {sql[:52]:<52} ({str(exc)[:60]})")
            continue
        failures.append(f"database ALLOWED: {sql!r}")
        print(f"  ALLOWED  {sql}  <-- FAILURE")

    print("\nLayer 2 - escalation is ineffective (session default flipped, write retried)")
    for escalation in ESCALATIONS:
        try:
            db.run_internal(escalation)
            outcome = "statement succeeded"
        except QueryError:
            outcome = "statement blocked"

        # Same statement, then a write in the SAME transaction.
        try:
            db.run_internal(f"{escalation}; CREATE TABLE pg_mcp_write_probe2 (a int)")
            failures.append(f"escalation+write ALLOWED: {escalation!r}")
            same_tx = "WRITE ALLOWED <-- FAILURE"
        except QueryError:
            same_tx = "write blocked"

        # And a write in a brand-new transaction, after the flip.
        try:
            db.run_internal("CREATE TABLE pg_mcp_write_probe3 (a int)")
            failures.append(f"post-escalation write ALLOWED after {escalation!r}")
            next_tx = "WRITE ALLOWED <-- FAILURE"
        except QueryError:
            next_tx = "write blocked"

        print(f"  {escalation[:48]:<48} {outcome}; same tx: {same_tx}; next tx: {next_tx}")

    print("\nLayer 2 - state check")
    info = db.run_internal(
        "SELECT current_setting('transaction_read_only'), "
        "current_setting('default_transaction_read_only'), current_user"
    )
    tx_ro, sess_ro, user = info.rows[0]
    print(f"  transaction_read_only={tx_ro}  default_transaction_read_only={sess_ro}  user={user}")
    if tx_ro != "on" or sess_ro != "on":
        failures.append("session is not read-only")

    print("\nSanity - a normal SELECT still works")
    rows = db.run_internal("SELECT count(*) FROM pg_class").rows
    print(f"  SELECT count(*) FROM pg_class -> {rows[0][0]}")

    db.close()

    if failures:
        print(f"\nFAILED: {len(failures)} probe(s) were not blocked:")
        for line in failures:
            print("  " + line)
        return 1
    print("\nPASS: all write probes blocked at both layers; reads work.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
