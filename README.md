# AlphaEvolve SQL Optimizer Skill

AlphaEvolve SQL Optimizer is a skill that applies AlphaEvolve to SQL query
optimization. Given a database connection and a read-only query, it constructs a
correctness-gated experiment, establishes a performance baseline, runs the
evolutionary search via the `ae` CLI, and returns the fastest rewrite whose
result set is verified identical to that of the original query.

Supported databases: **PostgreSQL** and **ClickHouse**. Other databases can be
added by writing an adapter that handles four things for that database:
connecting, reading a query's column types, hashing its result set, and
measuring server-side execution time (see
[`references/databases.md`](references/databases.md)).

## Design

The skill separates three concerns: **scaffolding** (a thin local CLI),
**evaluation** (a self-contained per-database evaluator), and **evolution** (the
existing `ae` CLI + AlphaEvolve backend). Nothing reimplements AlphaEvolve; the
skill only turns "a slow query + a connection" into a runnable experiment and
interprets the results.

```
  assistant (Antigravity)                          Google Cloud
        │                                        ┌────────────────┐
        │ 1. sqlopt.py init  ── scaffolds ──►    │  AlphaEvolve   │
        │ 2. sqlopt.py baseline                  │   backend      │
        │                                        │ (generates SQL │
        ▼                                        │  candidates)   │
  experiment dir/                                └───────┬────────┘
    initial_program.py  (seed = slow SQL)      acquire  │  submit score
    evaluator.py        (4 gates, 1 database)◄──────────┼──────────►
    problem_description.md                             ae CLI
    baseline.json                             (local acquire→eval→submit loop)
        │                                              │
        └──────────────── evaluator connects ─────────┼──► your DB
                                                       ▼   (PostgreSQL / ClickHouse)
                          4 gates per candidate:
                          L1 syntax · L2 column signature ·
                          L3 order-agnostic result hash · L4 server-side timing
```

- **`tool/sqlopt.py`** — `init` generates the experiment directory; `baseline`
  measures and caches the baseline fingerprint; `list-databases` lists the
  supported databases.
- **`tool/templates/evaluator_<name>.py`** — one self-contained evaluator per
  database (four gates; `-1e12` failure sentinel). `init` copies one and
  substitutes the query. Each generated experiment targets a single database and
  has no shared imports, so it stays clean and portable.
- **Correctness before speed** — a rewrite is accepted only if its column
  signature (L2) and result hash (L3) match the baseline. "Faster but different"
  scores `-1e12`.
- **Local data and credentials** — the evaluator runs within your own
  environment; row data and credentials are not transmitted beyond it.
  AlphaEvolve receives only the SQL statement, schema and column names, and
  scalar scores.

## Prerequisites

The following setup is required to configure AlphaEvolve on Google Cloud.
For official step-by-step guidance on Project licensing, IAM Service Account impersonation,
and Discovery Engine API enablement, see the
[Google Cloud AlphaEvolve Setup Guide](https://docs.cloud.google.com/gemini/enterprise/docs/alphaevolve/developer-guide/environment-and-api-access-setup?hl=zh-cn#project-and-licensing).

**1. Install and authenticate the Google Cloud CLI**
([install guide](https://docs.cloud.google.com/sdk/docs/install-sdk)):

```bash
gcloud --version
gcloud auth application-default login
```

**2. Enable Gemini Enterprise and create an engine** in your project — follow the
[Gemini Enterprise quickstart](https://docs.cloud.google.com/gemini/enterprise/docs/quickstart-gemini-enterprise).
The engine ID is required in step 5.

**3. Install the AlphaEvolve `ae` CLI** (required — the skill drives AlphaEvolve
through it):

```bash
pip install "git+https://github.com/Google-Cloud-AI/alphaevolve-on-googlecloud.git#subdirectory=skills"
```

Or using `uv`:

```bash
uv tool install "git+https://github.com/Google-Cloud-AI/alphaevolve-on-googlecloud.git#subdirectory=skills"
ae version
```

**5. Configure and test `ae`:**

```bash
ae config --project=<PROJECT_ID> --engine=<ENGINE_ID> \
    --location=global --models=gemini-3.5-flash
ae --json engine list        # if you need to find the engine ID
ae --json config test        # must print success before continuing
```

**6. Install database drivers** into the `ae` CLI tool env:

```bash
# postgres / hologres
uv pip install --python ~/.local/share/uv/tools/ae-cli/bin/python 'psycopg[binary]'
# clickhouse
uv pip install --python ~/.local/share/uv/tools/ae-cli/bin/python clickhouse-connect
```

**7. Run the Preflight Check Tool:**

```bash
python3 tool/sqlopt.py check --db postgres
```

## Install the skill

### Via skills.sh (Recommended)

```bash
npx skills add yanbasic/alpha-evolve-sql-optimizer
```

### Via Git Clone (Antigravity / Gemini)

```bash
git clone https://github.com/yanbasic/alpha-evolve-sql-optimizer.git ~/.gemini/config/skills/alpha_evolve_sql_optimizer
```

### Manual Installation

Copy the skill directory into your assistant's skills folder:

```bash
cp -r alpha-evolve-sql-optimizer ~/.gemini/config/skills/alpha_evolve_sql_optimizer
```

## Verify

Restart the assistant session, then run `/skills`. `alpha-evolve-sql-optimizer`
should appear in the list. Trigger it with a request such as "optimize this SQL"
/ "优化这条SQL".

## Usage

Open a terminal and start Antigravity with `agy`, then enter a prompt that
includes:

- the target database (e.g. postgres);
- the full SQL to optimize;
- optional hints (table sizes, indexes, version) that help the model.

For example:

```
Optimize this postgres SQL:
SELECT
    b.bid,
    b.bbalance,
    (SELECT COUNT(*)      FROM pgbench_accounts a WHERE a.bid = b.bid) AS account_count,
    (SELECT AVG(abalance) FROM pgbench_accounts a WHERE a.bid = b.bid) AS avg_balance,
    (SELECT SUM(abalance) FROM pgbench_accounts a WHERE a.bid = b.bid AND a.abalance > 0) AS positive_balance,
    (SELECT COUNT(*)      FROM pgbench_accounts a WHERE a.bid = b.bid AND a.abalance > 1000) AS large_count
FROM pgbench_branches b
ORDER BY b.bid
```

The skill then follows an interactive, human-in-the-loop workflow:

1. **Preflight Check (`sqlopt.py check`)**: Automatically verifies AlphaEvolve API permissions, engine setup, and DB drivers.
2. **Scaffold & Baseline (`sqlopt.py init / baseline`)**: Scaffolds the experiment directory and computes the 4-gate baseline runtime and result hash fingerprint.
3. **Interactive Review & Confirmation Panel (Submit)**: Renders a structured experiment summary in the terminal. The evolutionary loop **only starts after you explicitly choose to Submit**:
   - `[1]` Confirm & Submit optimization
   - `[2]` Adjust search budget / concurrency / timeout
   - `[3]` Edit schema & index hints
   - `[4]` Cancel
4. **Evolutionary Search (`ae run`)**: Runs the local acquire-evaluate-submit loop, reporting progress while AlphaEvolve evolves rewrites.
5. **Interactive Results Review**: Presents the best equivalent rewrite, measured speedup, and 4-gate verification status, along with options to view `EXPLAIN` diffs or export reports.

## Files

```
sql_optimizer/
├── SKILL.md                       # workflow (name: alpha-evolve-sql-optimizer)
├── README.md                      # this file
├── references/
│   ├── databases.md              # connection vars, drivers, per-database notes, adapter contract
│   ├── launch.md                  # exact ae create/start/run
│   └── troubleshooting.md
└── tool/
    ├── sqlopt.py                  # init / baseline / list-databases
    └── templates/
        ├── evaluator_postgres.py
        ├── evaluator_hologres.py
        ├── evaluator_clickhouse.py
        ├── initial_program.py.tmpl
        └── problem_description.md.tmpl
```

## Security

- **Row data is processed locally.** Query results are hashed and timed inside
  the evaluator process within your environment; no rows are transmitted
  externally.
- **Credentials are used locally only.** Connection details are read by the local
  evaluator from the experiment's `.env` or the shell environment and are not
  transmitted to AlphaEvolve. Setting them in the shell beforehand also keeps
  them out of the assistant conversation (see FAQ).
- **Transmitted to AlphaEvolve:** the SQL statement, schema and column names (via
  `problem_description.md`), generated candidate SQL, and scalar scores — the
  minimum required to generate rewrites.
- **Not transmitted to AlphaEvolve:** row data, connection endpoints, or
  credentials.
- **Read-only access.** A read-only database account is recommended; the L1 gate
  additionally rejects DDL and DML, restricting candidates to read-only queries.
- **Review before deployment.** Evolved queries are recommendations; a DBA should
  review and shadow-test them prior to production use.

## FAQ

**Which databases are supported, and how do I add another?**

Supported by default: PostgreSQL and ClickHouse. Adding another is a development
task, not a runtime setting: someone implements an adapter
(`tool/templates/evaluator_<name>.py`) covering connection, read-only handling,
column signature, an order-agnostic result hash, and server-side timing, and
installs that database's driver into the `ae` CLI tool env (drivers are not
installed automatically). A database qualifies only if it can satisfy that
contract. See `references/databases.md`.

**How do I configure the database connection and credentials?**

Provide them in your request, or when the skill prompts for them during its
connection-confirmation step — for example:

```
connect to postgres at db.internal:5432, database analytics, user readonly
```

The skill applies them to the experiment and re-measures the baseline before
evolution begins.

**How can credentials be configured without entering them in the conversation?**

1. Export the connection variables in a terminal — for PostgreSQL/Hologres:

   ```bash
   export PGHOST=127.0.0.1 PGPORT=5432 PGUSER=readonly PGPASSWORD=... PGDATABASE=analytics
   ```

   ClickHouse uses the `CH_*` variables (see `references/databases.md`).
2. Start the assistant from the same shell (`agy`) so it inherits these
   variables.
3. In your request, state that the connection is already configured — do not
   include the credentials.

The local evaluator reads the variables from the environment at run time; they
are therefore neither entered in the conversation nor transmitted to AlphaEvolve.
Alternatively, populate the experiment's `.env` directly in a terminal.

**Can I increase the search effort (try more rewrites)?**

Yes — state it in your request (e.g. "try more candidates" or "run 50
programs"), and the skill raises the AlphaEvolve search budget accordingly when
it launches. Larger budgets take longer.

**Can I inspect or re-run just one stage?**

Ask the assistant, e.g. "recompute the baseline" or "show me the generated
experiment". It runs the corresponding step without redoing the whole flow.

**How do I run the underlying steps directly (e.g. for debugging)?**

The same steps the skill performs can be run from the command line, which is
useful for inspecting a stage in isolation. Use the `ae` tool interpreter so the
driver is available:

```bash
AE_PY=~/.local/share/uv/tools/ae-cli/bin/python
$AE_PY tool/sqlopt.py list-databases
$AE_PY tool/sqlopt.py init --db postgres --sql-file slow.sql --out /tmp/exp
$AE_PY tool/sqlopt.py baseline --dir /tmp/exp   # after filling /tmp/exp/.env
```

Then launch with the `ae` CLI (see `references/launch.md`).

**How do I troubleshoot?**

The skill reports the error and stops rather than presenting a bad rewrite, and
surfaces the cause. Common issues and fixes — driver not found, connection
errors, timeouts, result-hash mismatch, empty leaderboard — are in
`references/troubleshooting.md`.
