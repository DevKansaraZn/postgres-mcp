# postgres-readonly — MCP server

Gives Claude **read-only** access to a PostgreSQL database: schema
introspection plus SELECT-only query execution, over MCP stdio.

Ask questions in plain English and let the model explore the schema and write
the SQL — without any way to modify your data.

Works with Claude Code, Claude Desktop, and any other MCP client.

---

## Safety model

Writes are blocked by four independent layers. Layers 1–3 are built in; layer 4
is a one-time DBA step and is the one to insist on for shared or production use.

| # | Layer | What it stops |
|---|-------|---------------|
| 1 | **SQL guard** (`pg_mcp/guard.py`) | Rejects anything that is not a single, non-locking `SELECT`/`WITH`/`TABLE`/`VALUES`/`SHOW`, before it is sent. |
| 2 | **Read-only session** | Connects with `default_transaction_read_only=on`. |
| 3 | **Read-only transaction, snapshot-pinned** | Every call runs in a `READ ONLY` transaction that is always rolled back. A trivial `SELECT 1` is issued first so the transaction cannot be promoted with `SET TRANSACTION READ WRITE`. |
| 4 | **Dedicated DB role** *(recommended)* | Postgres itself refuses writes even if the process is compromised. See below. |

The guard specifically rejects:

- `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `TRUNCATE`, `COPY`
- all DDL (`CREATE`, `ALTER`, `DROP`, `REFRESH`, `REINDEX`, `VACUUM`, `CLUSTER`)
- `GRANT` / `REVOKE` / `COMMENT ON` / `SECURITY LABEL`
- writable CTEs — `WITH x AS (DELETE ... RETURNING *) SELECT * FROM x`
- `SELECT ... INTO` (which creates a table)
- locking reads — `FOR UPDATE`, `FOR SHARE`, `FOR NO KEY UPDATE`, `FOR KEY SHARE`
- session/transaction control — `SET`, `RESET`, `BEGIN`, `COMMIT`, `DO`, `CALL`, `PREPARE`, `EXECUTE`
- **multiple statements** — `SELECT 1; DROP TABLE t` is one call, and it is refused
- dangerous functions — `pg_read_file`, `lo_export`, `dblink`, `pg_sleep`,
  `pg_terminate_backend`, `set_config`, advisory locks, replication functions

Keywords are matched only after string literals, dollar-quoted bodies, quoted
identifiers and comments have been blanked out. So
`SELECT * FROM events WHERE action = 'delete'` works fine, while
`SELECT 'a\' ; DROP TABLE t -- '` is still caught.

### Verify it yourself

```bash
.venv/bin/python tests/test_guard.py     # 81 guard checks, no DB needed
.venv/bin/python verify_readonly.py      # live probes against your DB
```

`verify_readonly.py` sends real write statements — through the guard, and again
with the guard deliberately bypassed — and asserts every one is refused.
Nothing is created or modified.

### Layer 4: a dedicated read-only role

Run once as a superuser. This is what makes read-only a *database* guarantee
rather than an application promise, and it is what you want before pointing the
server at anything that matters.

```sql
CREATE ROLE app_readonly LOGIN PASSWORD 'choose-a-strong-one';
ALTER ROLE app_readonly SET default_transaction_read_only = on;

GRANT CONNECT ON DATABASE your_database TO app_readonly;

-- Repeat per schema you want exposed.
GRANT USAGE ON SCHEMA public TO app_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO app_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO app_readonly;

-- Make sure nothing was inherited from PUBLIC.
REVOKE CREATE ON SCHEMA public FROM app_readonly;
```

Then set `POSTGRES_USER=app_readonly` and its password in `.env`.

---

## Setup

Requires Python 3.10+ and a reachable PostgreSQL server.

```bash
git clone https://github.com/<your-account>/postgres-mcp.git
cd postgres-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # then fill in your credentials
```

Your credentials live only in `.env`, which is gitignored and never leaves your
machine. Check it can connect:

```bash
.venv/bin/python -c "from pg_mcp.server import database_info; print(database_info())"
```

## Connect it to Claude Code

From the directory you cloned into:

```bash
claude mcp add postgres-readonly --scope user -- "$(pwd)/.venv/bin/python" "$(pwd)/server.py"
```

`--scope user` makes it available in all your projects. Confirm, then restart
Claude Code:

```bash
claude mcp list
```

### Claude Desktop

Add to your `claude_desktop_config.json` (**Settings → Developer → Edit Config**),
using absolute paths:

```json
{
  "mcpServers": {
    "postgres-readonly": {
      "command": "/absolute/path/to/postgres-mcp/.venv/bin/python",
      "args": ["/absolute/path/to/postgres-mcp/server.py"]
    }
  }
}
```

On Windows, use `.venv\\Scripts\\python.exe`.

---

## Sharing it with a team

The repo holds no secrets (`.env` is gitignored), so it can be shared as-is.

1. Each person clones the repo and runs the Setup steps with **their own**
   credentials — ideally a dedicated read-only role, not a personal superuser
   login.
2. In the project you want it available in, copy `.mcp.json.example` to
   `.mcp.json` and commit it.
3. Each person sets `POSTGRES_MCP_HOME` to wherever they cloned this repo.

`.mcp.json` is project-scoped and shareable: everyone on the repo gets the tool,
each against their own credentials. Claude Code asks each person to approve the
server the first time it loads.

---

## Tools

| Tool | Purpose |
|------|---------|
| `list_schemas` | Schemas with owner and object count. |
| `list_tables` | Tables/views, filterable by schema and name substring; shows estimated rows and size. |
| `describe_table` | Columns, types, nullability, defaults, constraints, indexes, view definition. |
| `search_columns` | Find which tables carry a given column name. |
| `sample_rows` | First *n* rows of a table. |
| `run_query` | Run a SELECT. `max_rows` and `timeout_ms` are per-call overrides. |
| `explain_query` | Query plan; `analyze=true` runs it and reports real timings. |
| `database_info` | Version, current database/user, limits, read-only confirmation. |

Results are capped at `PG_MCP_MAX_ROWS` (default 200) and fetched through a
server-side cursor, so a `SELECT *` on a huge table pulls only what is shown
rather than streaming the whole relation into memory. Truncation is always
stated in the output.

## Configuration

All settings and their defaults are documented in [`.env.example`](.env.example).

Connection settings resolve in this order:

1. `DATABASE_URL` — a full connection URL
2. `POSTGRES_HOST` / `POSTGRES_PORT` / `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB`
3. The standard libpq variables — `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, `PGDATABASE`

Every `PG_MCP_*` variable overrides all of the above, so you can point one
checkout at a different database per MCP client without touching `.env`.

## Layout

```
postgres-mcp/
├── server.py              # entry point (= python -m pg_mcp)
├── verify_readonly.py     # live proof that writes are refused
├── pg_mcp/
│   ├── config.py          # env loading, limits, DSN redaction
│   ├── guard.py           # the SQL guard (layer 1)
│   ├── db.py              # read-only connection & execution (layers 2-3)
│   ├── render.py          # result formatting
│   └── server.py          # MCP tool definitions
└── tests/test_guard.py    # guard test suite
```

## Troubleshooting

**Server does not appear in Claude** — run the command from `claude mcp list`
by hand; startup errors print to stderr. A config problem exits with code 2 and
a message naming the missing variable.

**`connection refused`** — check the database is running and the port matches.
If it runs in Docker, make sure the container is up and the port is published.

**A legitimate query is rejected** — the guard errs on the side of refusing.
Quote the identifier (`"start"`, `"comment"`) if a column name collides with a
blocked keyword, and open an issue if it is a real false positive.

**`statement timeout`** — raise `timeout_ms` on the call, or
`PG_MCP_STATEMENT_TIMEOUT_MS` in `.env` (hard cap 600 s). Run `explain_query`
first on a large table.

## Contributing

Bug reports and pull requests are welcome. If you are changing `guard.py`, add
cases to `tests/test_guard.py` — both a query that must be allowed and one that
must be rejected — and make sure `verify_readonly.py` still passes against a
real database.
