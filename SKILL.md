---
name: alpha-evolve-sql-optimizer
description: >
  Optimize a slow SQL query with AlphaEvolve interactively with human-in-the-loop.
  Guides the user through a structured, multi-step interactive wizard with selectable options:
  verifying AE permissions, selecting database engines and connection methods, formulating
  evolution seeds and query hints, choosing evaluation parameters and search budgets,
  computing 4-gate baselines, and presenting a final review panel before submitting to AlphaEvolve.
  Supports PostgreSQL, ClickHouse, and Hologres; extensible to other databases.
  Triggers on: "optimize this SQL", "optimize this query", "make this
  query faster", "tune this SQL", "slow query", "speed up this query".
---

# AlphaEvolve SQL Optimizer (Interactive Wizard Protocol)

You guide the user through optimizing slow SQL queries using Google Cloud AlphaEvolve,
maintaining a strictly **step-by-step, interactive wizard with selectable options**.
You never execute the evolutionary loop automatically without explicit user confirmation.
Correctness is strictly enforced by a four-gate evaluator; a rewrite is only considered
an improvement if its result set is verified identical to the baseline.

The database-specific work is done by the bundled tool `tool/sqlopt.py`.
Evolution orchestration uses the standard AlphaEvolve `ae` CLI.

---

## Critical Rules

1. **Zero Redundant Prompts & Context Auto-Detection.** If the user has already specified the database engine (e.g., Oracle, PostgreSQL, ClickHouse, MySQL), connection method, or the SQL query in their request, **DO NOT ask the user to re-select or re-enter that information**. Immediately auto-bind the provided details and proceed to the next unfulfilled step. Never mention sample tables (e.g., `pgbench`) unless they are actually in the user's query.
2. **Strict Multiple-Choice Protocol.** When asking for missing parameters, always provide concrete numbered options `[1], [2], [3]...` with sensible default values. **NEVER prompt the user with open-ended questionnaires or ask them to type out multi-line bulleted configuration fields.**
3. **Zero Credential Exposure.** NEVER print usernames, passwords, access keys, or raw secrets in chat transcripts, review panels, or reports. Credentials are read exclusively from process environment variables or the workspace's local `.env` file. Display only masked endpoints (e.g. `Host: 127.0.0.1:5432 | Database: analytics | Credentials: [LOADED FROM ENVIRONMENT]`).
4. **Preflight first.** Always verify AlphaEvolve API permissions, engine configuration, and DB drivers before touching SQL or databases.
5. **User in the loop (Submit gate).** Never launch the `ae` search loop unattended without presenting an experiment summary card and receiving the user's explicit confirmation (`Submit`).
6. **Never run candidate SQL yourself.** All candidate execution goes through the sandboxed `evaluator.py`.
7. **Correctness before speed.** A rewrite is only valid if it passes all four gates (L1 Syntax, L2 Column Signature, L3 Result Hash, L4 Timing). `-1e12` is the failure sentinel.
8. **Data stays local.** Only the SQL query text, schema, and column names reach AlphaEvolve (via `problem_description.md`). Table row data never leaves the local evaluator process.
9. **Human review before production.** Evolved rewrites are recommendations. Remind the user that a DBA should review and shadow-test them before production deployment.

---

## Interactive Wizard Protocol

```
[Step 1: Preflight Check] ──► Verify GCP Auth, AE Engine API, Licensing, and DB Drivers
         │
[Step 2: Engine & Connect]──► Select Engine & Credential Configuration via Numbered Options
         │
[Step 3: Query & Seed]    ──► Input slow SQL, choose seed strategy & provide schema hints
         │
[Step 4: Search Budget]   ──► Select evaluation preset (Fast / Standard / Deep / Custom)
         │
[Step 5: Baseline Gate]   ──► Scaffold experiment & compute 4-gate baseline runtime/hash
         │
[Step 6: Submit Panel]    ──► Review full experiment card -> User confirms: [1] Submit
         │
[Step 7: Evolution & Next]──► Stream ae progress, deliver best rewrite, offer next actions
```

---

### Step 1: Preflight & Environment Verification

Execute the preflight check before processing queries:

```bash
AE_PY=~/.local/share/uv/tools/ae-cli/bin/python
$AE_PY tool/sqlopt.py check
```

Present the diagnostic status table to the user.
- If all checks pass: Present options to proceed to Step 2.
- **If `ae` CLI is missing from PATH:** Offer assisted automated installation:
  ```text
  AlphaEvolve (ae) CLI is not detected in your environment.
  Would you like me to install it for you?
    [1] Yes, install via pip (pip install "git+https://github.com/Google-Cloud-AI/alphaevolve-on-googlecloud.git#subdirectory=skills")
    [2] Yes, install via uv tool (uv tool install "git+https://github.com/Google-Cloud-AI/alphaevolve-on-googlecloud.git#subdirectory=skills")
    [3] Skip for now
  ```
  Upon user selection `[1]` or `[2]`, execute the installation command, verify with `ae version`, and proceed to Step 2.
- If any check fails (e.g. `gcloud auth`, `ae config test`, Engine ID, licensing, DB drivers): Provide exact remediation referencing the [Google Cloud AlphaEvolve Setup Guide](https://docs.cloud.google.com/gemini/enterprise/docs/alphaevolve/developer-guide/environment-and-api-access-setup?hl=zh-cn#project-and-licensing).

---

### Step 2: Target Database Architecture & Credential Configuration

**Context Auto-Binding:** If the database engine was already specified in the user request (e.g., "Optimize this Oracle query"), **skip engine selection completely**, bind the target engine to Oracle, auto-provision driver if missing, and proceed directly to credential verification.

Only if the engine was omitted or ambiguous, present the selection menu:

```text
Select Target Database Engine:
  [1] PostgreSQL (Default port: 5432)
  [2] ClickHouse (Default port: 8123)
  [3] Hologres   (Default port: 80)
  [4] MySQL      (Default port: 3306)
  [5] Oracle     (Default port: 1521 / TNS DSN)
  [6] SQL Server (Default port: 1433 / ODBC)
```

**Driver Verification & Assisted Auto-Installation:**
If the database driver for the chosen engine is missing from the active Python or `ae-cli` environment, offer assisted auto-installation:

```text
Database driver for '<engine>' (<package>) is not installed.
Would you like me to install it for you?
  [1] Yes, install driver automatically (Recommended)
  [2] No, I will install it manually
```

Upon user selection `[1]`, execute `$AE_PY tool/sqlopt.py install-driver <engine>`, verify the driver import, and continue to credential verification.

**Handling Missing Environment Variables:**
If credentials are not already set in the shell environment, automatically initialize `<exp_dir>/.env` and display the exact syntax template for the selected engine:

```text
Environment configuration not detected. Initialized template at: <exp_dir>/.env

Format for selected engine:
----------------------------------------------------------------------------
# PostgreSQL / Hologres:
PGHOST=127.0.0.1
PGPORT=5432
PGUSER=readonly_user
PGPASSWORD=your_password
PGDATABASE=analytics

# ClickHouse:
CH_HOST=127.0.0.1
CH_PORT=8123
CH_USER=default
CH_PASSWORD=your_password
CH_DATABASE=default

# Oracle Database (Easy Connect or TNS):
ORACLE_USER=readonly_user
ORACLE_PASSWORD=your_password
ORACLE_DSN=127.0.0.1:1521/ORCLPDB1

# Microsoft SQL Server (MSSQL / ODBC):
MSSQL_HOST=127.0.0.1
MSSQL_PORT=1433
MSSQL_USER=readonly_user
MSSQL_PASSWORD=your_password
MSSQL_DATABASE=analytics
MSSQL_DRIVER=ODBC Driver 18 for SQL Server
----------------------------------------------------------------------------
Select Action:
  [1] Credentials populated in <exp_dir>/.env -> Proceed to Step 3
  [2] Inherit from shell environment variables
  [3] Abort
```

---

### Step 3: Slow SQL Query & Seed Formulation

Collect and validate the read-only SQL query:
1. **Query Input**: Provide via file path or pasted SQL block (single read-only `SELECT` / `WITH`).
2. **Evolution Seed Strategy**:
   - `[1] Single Seed (Recommended)`: Original query serves as baseline seed `OPTIMIZED_SQL` (score ≈ 1.0).
   - `[2] Pre-warmed Seed`: Original query with initial heuristic hints (e.g. unnested CTEs or subqueries).
3. **Optimization Hints**: Ask user for table row counts, index definitions, database versions, or known bottlenecks.

---

### Step 4: Search Budget & Evaluation Parameters

Present optimization parameter presets to the user:

```text
Please select an optimization profile:
  [1] Feasibility Verification      (10 programs, concurrency: 2, timeout: 60s)  - Rapid validation
  [2] Standard Evolutionary Search  (20 programs, concurrency: 3, timeout: 120s) - Recommended production profile
  [3] Exhaustive Global Optimization(50 programs, concurrency: 5, timeout: 300s) - Maximum search exploration
  [4] Custom Parameter Profile      (Manual specification of budget, concurrency, and timeout)
```

---

### Step 5: Scaffolding & 4-Gate Baseline Measurement

1. **Scaffold Experiment Directory**:
   ```bash
   $AE_PY tool/sqlopt.py init --db <name> \
       --sql-file <slow.sql> --out <exp_dir> --title "<short title>" \
       --hints "<schema hints>"
   ```
2. **Compute Baseline**:
   ```bash
   $AE_PY tool/sqlopt.py baseline --dir <exp_dir> --timeout-ms <timeout_ms>
   ```
   Verify `baseline.json` is generated, measuring the 4 gates (L1 Syntax, L2 Column Signature, L3 Result Hash, L4 Timing).

---

### Step 6: Final Review & Submit Decision Panel

Display the structured confirmation panel and wait for user selection before launching:

```text
+----------------------------------------------------------------------------+
| AlphaEvolve SQL Optimizer Experiment Review                                |
+----------------------------------------------------------------------------+
| Target Database:      <db_name> (<endpoint> / <dbname>)                    |
| Baseline Runtime:     <baseline_median_ms> ms (L1-L4 verified)             |
| Search Budget:        <max_programs> programs (concurrency: <concurrency>) |
| Per-Candidate Timeout:<timeout_sec>s                                       |
| Model:                gemini-3.5-flash                                     |
| Optimization Goal:    Minimize latency while preserving identical output   |
| Schema & Hints:       <hints_summary>                                      |
+----------------------------------------------------------------------------+

Confirm to submit and launch AlphaEvolve optimization?
  [1] Confirm & Submit (Start Evolution Loop)
  [2] Adjust Parameters (Go back to Step 4)
  [3] Edit Schema & Index Hints (Go back to Step 3)
  [4] Cancel Experiment
```

- If user selects **[1]**, proceed to Step 7.
- If user selects **[2]** or **[3]**, update configuration and re-display panel.
- If user selects **[4]**, cleanly abort.

---

### Step 7: Evolution Monitoring & Results Delivery

1. **Launch AlphaEvolve Loop**:
   ```bash
   ae --json experiment create --max-programs <N> --concurrency <C> --problem-file <exp_dir>/problem_description.md --title "<title>" --models gemini-3.5-flash
   ae --json experiment start <nickname> --program-dir <exp_dir> --score "<seed_score>"
   ae --json experiment run <nickname> --evaluator <exp_dir>/evaluator.py --backend local --dashboard <nickname>-dashboard.md --timeout <T>
   ```
2. **Deliver Results**:
   - **Optimized SQL**: Highlighted rewrite with diff breakdown.
   - **Performance Metrics**: Baseline vs Optimized latency, speedup factor, and proof that 4 verification gates passed.
   - **DBA Review Note**: Remind user to shadow-test with `EXPLAIN ANALYZE` before production.

**Interactive Next Steps**:
```text
What would you like to do next?
  [1] View detailed EXPLAIN / EXPLAIN ANALYZE comparison
  [2] Deepen search with higher budget (e.g. 50-100 programs)
  [3] Export formal optimization report (optimization_results.md)
  [4] Optimize another SQL query
```

---

## Autonomous Database Provisioning & Dynamic Adapter Synthesis

When the user requests optimization for a database engine without an existing pre-built adapter (e.g. MySQL, Oracle, MSSQL, Snowflake, DuckDB, Trino, SQLite):

1. **Auto-Provision Driver**:
   Run driver auto-installation into both the active Python environment and the `ae-cli` environment:
   ```bash
   AE_PY=~/.local/share/uv/tools/ae-cli/bin/python
   $AE_PY tool/sqlopt.py install-driver <engine_name>
   ```

2. **Synthesize Database Adapter (`tool/templates/evaluator_<engine>.py`)**:
   Synthesize a self-contained evaluator following the 5-point adapter contract:
   - `_connect()`: Ingests credentials from `<ENGINE>_*` environment variables or `.env`.
   - `_set_readonly(conn)`: Configures read-only session isolation.
   - `_get_column_signature(conn, sql, timeout_ms)`: Fast schema inspection (`LIMIT 0`).
   - `_hash_result_set(conn, sql, timeout_ms)`: Deterministic, order-agnostic multiset hash.
   - `_measure_execution_ms(conn, sql, timeout_ms)`: Server-side execution duration.

3. **Validate Connection & Execute Baseline**:
   Initialize `<exp_dir>/.env` using `sqlopt.py init --db <engine_name>` and execute `sqlopt.py baseline --dir <exp_dir>`.

---

## References

| File                          | When to read                                      |
| ----------------------------- | ------------------------------------------------- |
| `references/databases.md`     | Connection variables, drivers, adapter contract  |
| `references/launch.md`        | Detailed `ae` CLI create/start/run flag reference |
| `references/troubleshooting.md`| Diagnosing AE permissions, drivers, and timeouts  |
