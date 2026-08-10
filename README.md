# AlphaEvolve SQL Optimizer

AlphaEvolve SQL Optimizer applies Google Cloud AlphaEvolve to SQL query optimization through a structured, human-in-the-loop engineering workflow. Given a target database connection and a read-only SQL query, it constructs a four-gate verification harness, establishes a reproducible latency baseline, orchestrates evolutionary candidate generation via AlphaEvolve, and delivers the fastest semantically equivalent rewrite verified against the live database.

---

## Key Features

* **Built-in Database Adapters:** Native support for **PostgreSQL**, **ClickHouse**, **Hologres**, and **Oracle**.
* **Autonomous Driver & Environment Provisioning:** Automated installation of the AlphaEvolve (`ae`) CLI and required database drivers (`psycopg`, `clickhouse-connect`, `oracledb`, `pyodbc`, `pymysql`, `duckdb`, `snowflake-connector-python`).
* **Extensible Adapter Contract:** Rapidly synthesize evaluators for MySQL, SQL Server, Snowflake, DuckDB, SQLite, Trino, and custom SQL dialects.
* **Rigorous 4-Gate Verification:**
  * **Gate L1 (Syntax & Read-Only Safety):** Blocks DDL, DML, and multi-statement queries.
  * **Gate L2 (Column Signature):** Verifies column names, types, and ordering.
  * **Gate L3 (Result Set Fingerprint):** Computes an order-agnostic cryptographic multiset hash to prove identical output.
  * **Gate L4 (Server-Side Latency Measurement):** Measures precise database query execution time in milliseconds.
* **Human-in-the-Loop Review Gate:** Structured review panels with multiple-choice configuration options; search loops only dispatch after explicit user confirmation (`Submit`).
* **Zero Credential Exposure:** Database credentials and row data remain strictly local within the evaluation sandbox and are never transmitted to AlphaEvolve or printed in plaintext.

---

## Architectural Workflow

```
       Assistant (AI Agent / CLI Wizard)                        Google Cloud
               │                                              ┌────────────────┐
               │ 1. Preflight Check (sqlopt.py check)         │  AlphaEvolve   │
               │ 2. Scaffolding (sqlopt.py init) ─────────►   │   Backend      │
               │ 3. Baseline Measurement (sqlopt.py baseline) │ (Generates SQL │
               │ 4. Human-in-the-Loop Submit Gate             │  Candidates)   │
               ▼                                              └───────┬────────┘
       Experiment Directory/                                  Acquire │  Submit Score
         initial_program.py    (Reference baseline seed) ◄────────────┼──────────►
         evaluator.py          (4 verification gates)               ae CLI
         problem_description.md(Schema & hints context)   (Local acquire-evaluate-submit)
         baseline.json         (Cached runtime & hash)                │
               │                                                      │
               └──────────────── Evaluator Connects ──────────────────┼──► Target Database
                                                                      ▼   (Postgres, Oracle,
                                 4 Verification Gates:                     ClickHouse, etc.)
                                 L1 Syntax · L2 Signature ·
                                 L3 Multiset Hash · L4 Timing
```

---

## Prerequisites

Before using AlphaEvolve SQL Optimizer, ensure your Google Cloud environment is configured:

1. **Google Cloud SDK Authentication** ([Install Guide](https://docs.cloud.google.com/sdk/docs/install-sdk)):
   ```bash
   gcloud --version
   gcloud auth application-default login
   ```

2. **Gemini Enterprise & AlphaEvolve Engine:**
   Ensure Gemini Enterprise licensing and Discovery Engine API are enabled on your project ([Setup Guide](https://docs.cloud.google.com/gemini/enterprise/docs/alphaevolve/developer-guide/environment-and-api-access-setup?hl=zh-cn#project-and-licensing)).

3. **Install AlphaEvolve (`ae`) CLI:**
   ```bash
   pip install "git+https://github.com/Google-Cloud-AI/alphaevolve-on-googlecloud.git#subdirectory=skills"
   ```
   Or via `uv tool`:
   ```bash
   uv tool install "git+https://github.com/Google-Cloud-AI/alphaevolve-on-googlecloud.git#subdirectory=skills"
   ae version
   ```

4. **Configure `ae` CLI:**
   ```bash
   ae config --project=<PROJECT_ID> --engine=<ENGINE_ID> --location=global --models=gemini-3.5-flash
   ae --json config test
   ```

---

## Installation

### Via skills.sh (Recommended)

```bash
npx skills add yanbasic/alpha-evolve-sql-optimizer
```

### Via Git Clone (Antigravity / Gemini)

```bash
git clone https://github.com/yanbasic/alpha-evolve-sql-optimizer.git ~/.gemini/config/skills/alpha_evolve_sql_optimizer
```

---

## Usage

Trigger the skill in your AI assistant with a natural prompt:

```text
Optimize this PostgreSQL query:
SELECT
    b.bid,
    b.bbalance,
    (SELECT COUNT(*) FROM pgbench_accounts a WHERE a.bid = b.bid) AS account_count,
    (SELECT AVG(abalance) FROM pgbench_accounts a WHERE a.bid = b.bid) AS avg_balance
FROM pgbench_branches b
ORDER BY b.bid;
```

The assistant automatically detects the database engine, verifies drivers and environment credentials, computes baseline metrics, and presents a structured review panel before submitting to AlphaEvolve.

---

## Database Authentication & Environment Variables

All evaluators load credentials dynamically from process environment variables or the workspace's `.env` configuration file. Plaintext passwords are never printed in transcripts or logs.

| Database | Connection Variables | Python Driver |
| :--- | :--- | :--- |
| **PostgreSQL** | `PGHOST`, `PGPORT` (5432), `PGUSER`, `PGPASSWORD`, `PGDATABASE` | `psycopg[binary]` |
| **Hologres** | `PGHOST`, `PGPORT` (80), `PGUSER`, `PGPASSWORD`, `PGDATABASE` | `psycopg[binary]` |
| **ClickHouse** | `CH_HOST`, `CH_PORT` (8123), `CH_USER`, `CH_PASSWORD`, `CH_DATABASE`, `CH_SECURE` | `clickhouse-connect` |
| **Oracle** | `ORACLE_USER`, `ORACLE_PASSWORD`, `ORACLE_DSN` (`host:port/service_name`), `ORACLE_WALLET_LOCATION` | `oracledb` |
| **MySQL** | `MYSQL_HOST`, `MYSQL_PORT` (3306), `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DATABASE`, `MYSQL_DSN` | `pymysql` |
| **SQL Server** | `MSSQL_HOST`, `MSSQL_PORT` (1433), `MSSQL_USER`, `MSSQL_PASSWORD`, `MSSQL_DATABASE`, `MSSQL_DRIVER` | `pyodbc` |
| **Snowflake** | `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_PASSWORD`, `SNOWFLAKE_DATABASE`, `SNOWFLAKE_SCHEMA`, `SNOWFLAKE_WAREHOUSE` | `snowflake-connector-python` |

---

## CLI Reference (`tool/sqlopt.py`)

```bash
# Run the interactive optimization wizard
python3 tool/sqlopt.py wizard

# Execute preflight environment diagnostics
python3 tool/sqlopt.py check [--db <engine>]

# Auto-install AlphaEvolve CLI
python3 tool/sqlopt.py install-ae [--method pip|uv]

# Auto-provision database driver
python3 tool/sqlopt.py install-driver <engine>

# Scaffold a standalone experiment workspace
python3 tool/sqlopt.py init --db postgres --sql-file query.sql --out /tmp/exp_postgres --title "Account Query"

# Measure and record baseline metrics
python3 tool/sqlopt.py baseline --dir /tmp/exp_postgres --timeout-ms 120000

# List available native database adapters
python3 tool/sqlopt.py list-databases
```

---

## Repository Structure

```
alpha-evolve-sql-optimizer/
├── SKILL.md                              # Agent skill specification & wizard protocol
├── README.md                             # Architecture, setup, and usage documentation
├── LICENSE                               # Apache 2.0 License
├── references/
│   ├── databases.md                     # Connection specifications & adapter contract
│   ├── launch.md                         # AlphaEvolve CLI command reference
│   └── troubleshooting.md               # Diagnostic guide for API, IAM, and driver errors
└── tool/
    ├── sqlopt.py                         # CLI orchestrator, wizard, and driver manager
    └── templates/
        ├── evaluator_postgres.py         # PostgreSQL 4-gate evaluation adapter
        ├── evaluator_clickhouse.py       # ClickHouse 4-gate evaluation adapter
        ├── evaluator_hologres.py         # Hologres 4-gate evaluation adapter
        ├── evaluator_oracle.py           # Oracle 4-gate evaluation adapter
        ├── initial_program.py.tmpl       # Seed program template
        └── problem_description.md.tmpl   # Problem description markdown template
```

---

## Security & Data Privacy

* **Local Evaluation:** Query execution, result set hashing, and latency timing occur entirely within the local sandbox. Row data is never transmitted to Google Cloud or LLM endpoints.
* **Zero Credential Exposure:** Database passwords and connection secrets are loaded strictly into local process memory.
* **Read-Only Safety:** The L1 evaluation gate parses SQL syntax and strictly forbids DDL and DML operations (`INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `TRUNCATE`).
* **DBA Review:** Optimized queries are recommendations. Database administrators should inspect execution plans (`EXPLAIN ANALYZE`) and shadow-test queries before production deployment.

---

## License

Apache 2.0 — see [LICENSE](LICENSE) for details.
