# Databases

Each supported database is one self-contained adapter in
`tool/templates/evaluator_<name>.py`. `sqlopt.py init --db <name>` copies it and
substitutes the query; every generated experiment targets a single database.
`sqlopt.py list-databases` shows what is supported.

## Connection variables

The generated `.env.example` contains the right keys for the chosen database.
Copy it to `.env` and fill in. The evaluator and `sqlopt.py baseline` read these
from the environment (`.env` is loaded automatically by `baseline`).

### postgres

| Var | Meaning |
|-----|---------|
| `PGHOST` / `PGPORT` | host / port (default 5432) |
| `PGUSER` / `PGPASSWORD` | credentials (use a **read-only** role) |
| `PGDATABASE` | database name |

Driver: `psycopg[binary]`.

### hologres

Hologres speaks the PostgreSQL wire protocol, so it uses the same libpq vars and
the `psycopg` driver.

| Var | Meaning |
|-----|---------|
| `PGHOST` / `PGPORT` | the Hologres endpoint host / port (from the console) |
| `PGUSER` / `PGPASSWORD` | AccessKey ID / Secret (or a dedicated account) |
| `PGDATABASE` | Hologres database |

Driver: `psycopg[binary]`.

Notes:
- Timing (`_measure_execution_ms`) tries `EXPLAIN (ANALYZE, FORMAT JSON)`, then
  text `EXPLAIN ANALYZE`, then client wall-clock — because Hologres' EXPLAIN
  output differs from vanilla PostgreSQL across versions. The evaluator surfaces
  the execution engine (**HQE** native vs **PQE** Postgres-compat) in the
  timing insight when detectable; an HQE↔PQE switch alone can change latency by
  an order of magnitude, so watch for it when reading results.
- Result hash uses `to_jsonb(...)`. If your Hologres version rejects it, replace
  the inner select in `_hash_result_set` with an explicit `concat_ws('|',
  col1::text, col2::text, ...)` over the query's columns.
- **Status:** the shared psycopg path is validated against PostgreSQL; the
  Hologres-specific EXPLAIN parsing needs final sign-off on a real instance.

### clickhouse (incl. Alibaba Cloud ClickHouse)

| Var | Meaning |
|-----|---------|
| `CH_HOST` / `CH_PORT` | host / HTTP port (8123, or 8443 with TLS) |
| `CH_USER` / `CH_PASSWORD` | credentials (use a read-only profile) |
| `CH_DATABASE` | database |
| `CH_SECURE` | `true` for HTTPS/TLS, else `false` |

Driver: `clickhouse-connect` (pure Python).

### mysql

| Var | Meaning |
|-----|---------|
| `MYSQL_HOST` / `MYSQL_PORT` | host / port (default 3306) |
| `MYSQL_USER` / `MYSQL_PASSWORD` | credentials (read-only user) |
| `MYSQL_DATABASE` | database name |
| `MYSQL_DSN` | (Optional) Direct URI: `mysql+pymysql://user:pass@host:3306/db` |

Driver: `pymysql` or `mysql-connector-python`.

### oracle

| Var | Meaning |
|-----|---------|
| `ORACLE_USER` / `ORACLE_PASSWORD` | credentials (read-only account) |
| `ORACLE_DSN` | Easy Connect (`host:1521/service_name`) or TNS alias |
| `ORACLE_WALLET_LOCATION` | (Optional) Path to Oracle Cloud / Autonomous DB wallet directory |

Driver: `oracledb` (Thin mode, pure Python).

### sqlserver / mssql

| Var | Meaning |
|-----|---------|
| `MSSQL_HOST` / `MSSQL_PORT` | host / port (default 1433) |
| `MSSQL_USER` / `MSSQL_PASSWORD` | credentials (read-only user) |
| `MSSQL_DATABASE` | database name |
| `MSSQL_DRIVER` | ODBC driver name (e.g. `ODBC Driver 18 for SQL Server`) |
| `MSSQL_CONNECTION_STRING` | (Optional) Direct PyODBC DSN connection string |

Driver: `pyodbc` or `pymssql`.

## Installing the driver

The `ae` CLI runs the evaluator in its own uv-tool Python environment. Install
the database driver there, and run `sqlopt.py` with that same interpreter so
`baseline` can connect — otherwise the evaluator fails with `ModuleNotFoundError`:

```bash
uv pip install --python ~/.local/share/uv/tools/ae-cli/bin/python <driver>
```

`<driver>` = `'psycopg[binary]'` for postgres/hologres, `clickhouse-connect` for
clickhouse.

## Adding a new database (the adapter contract)

Copy `tool/templates/evaluator_postgres.py` →
`tool/templates/evaluator_<name>.py` and edit only the five spots marked
`ENGINE-SPECIFIC`:

| # | Function | Must do |
|---|----------|---------|
| 1 | driver import + `_connect()` | return a connection usable as `with ... as conn:` |
| 2 | `_set_readonly(conn)` | enforce session read-only (or no-op if per-query) |
| 3 | `_get_column_signature(conn, sql, timeout_ms)` | return ordered `((name, type), ...)`, cheaply (no full scan) |
| 4 | `_hash_result_set(conn, sql, timeout_ms)` | return a deterministic, **order-agnostic** fingerprint of the row multiset |
| 5 | `_measure_execution_ms(conn, sql, timeout_ms)` | return one server-side execution time in ms |

Everything else (L1 syntax gate, `-1e12` sentinel, `evaluate_program`,
`compute_baseline`, `main`) stays byte-for-byte. Then
`sqlopt.py init --db <name>` works and `list-databases` shows it. A database is
suitable only if it can satisfy all five — in particular an order-agnostic
result fingerprint and a server-side execution time.

## Extensible Credential Ingestion Protocol

To ensure credential privacy and seamless integration for new database engines:

1. **Standard Prefix Convention**:
   The new evaluator's `_connect()` should read connection parameters using standard environment variables:
   - `<ENGINE>_HOST`, `<ENGINE>_PORT`, `<ENGINE>_USER`, `<ENGINE>_PASSWORD`, `<ENGINE>_DATABASE`
   - Or a single connection URI: `<ENGINE>_DSN` / `DATABASE_URL`
2. **Scaffold Integration**:
   `sqlopt.py init` dynamically generates `<exp_dir>/.env.example` with the corresponding `<ENGINE>_*` keys.
3. **Zero Secret Exposure**:
   - Evaluators load credentials locally from process environment variables or `<exp_dir>/.env`.
   - Credentials are never printed in chat history, review panels, or logs. All summaries display masked connection metadata.
