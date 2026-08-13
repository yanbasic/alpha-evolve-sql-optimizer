#!/usr/bin/env python3
"""sqlopt — thin scaffolder for AlphaEvolve SQL-optimization experiments.

Turns "a slow query + a connection" into a ready-to-run experiment directory,
then computes its baseline fingerprint. AlphaEvolve orchestration itself is
still done by the `ae` CLI (create / start / run) — this tool does not wrap it.

Commands:
    sqlopt.py check [--db <name>]
    sqlopt.py init --db <name> --sql-file q.sql --out <dir> [--title T] [--hints H]
    sqlopt.py baseline --dir <dir> [--timeout-ms 120000]
    sqlopt.py list-databases

The database is always given explicitly (`--db`); it is never guessed from a
DSN. Supported databases correspond to the `templates/evaluator_<name>.py`
adapters. See references/databases.md.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent / "templates"

_ENV_TEMPLATES = {
    "postgres": (
        "# PostgreSQL Connection Configuration\n"
        "PGHOST=127.0.0.1\n"
        "PGPORT=5432\n"
        "PGUSER=readonly_user\n"
        "PGPASSWORD=your_password\n"
        "PGDATABASE=analytics\n"
        "# Optional direct URI: DATABASE_URL=postgresql://user:pass@127.0.0.1:5432/analytics\n"
    ),
    "hologres": (
        "# Hologres Connection Configuration (Postgres wire-compatible)\n"
        "PGHOST=your-hologres-endpoint.hologres.aliyuncs.com\n"
        "PGPORT=80\n"
        "PGUSER=your-access-key-id\n"
        "PGPASSWORD=your-access-key-secret\n"
        "PGDATABASE=your_database\n"
    ),
    "clickhouse": (
        "# ClickHouse Connection Configuration (HTTP/Native)\n"
        "CH_HOST=127.0.0.1\n"
        "CH_PORT=8123\n"
        "CH_USER=default\n"
        "CH_PASSWORD=your_password\n"
        "CH_DATABASE=default\n"
        "CH_SECURE=false\n"
    ),
    "mysql": (
        "# MySQL Connection Configuration\n"
        "MYSQL_HOST=127.0.0.1\n"
        "MYSQL_PORT=3306\n"
        "MYSQL_USER=readonly_user\n"
        "MYSQL_PASSWORD=your_password\n"
        "MYSQL_DATABASE=analytics\n"
        "# Optional direct URI: MYSQL_DSN=mysql+pymysql://user:pass@127.0.0.1:3306/analytics\n"
    ),
    "oracle": (
        "# Oracle Database Connection Configuration (Easy Connect or TNS DSN)\n"
        "ORACLE_USER=readonly_user\n"
        "ORACLE_PASSWORD=your_password\n"
        "# Easy Connect format: host:port/service_name (e.g. 127.0.0.1:1521/ORCLPDB1)\n"
        "ORACLE_DSN=127.0.0.1:1521/ORCLPDB1\n"
        "# Optional: ORACLE_WALLET_LOCATION=/path/to/wallet_dir\n"
    ),
    "sqlserver": (
        "# Microsoft SQL Server (MSSQL) Connection Configuration\n"
        "MSSQL_HOST=127.0.0.1\n"
        "MSSQL_PORT=1433\n"
        "MSSQL_USER=readonly_user\n"
        "MSSQL_PASSWORD=your_password\n"
        "MSSQL_DATABASE=analytics\n"
        "MSSQL_DRIVER=ODBC Driver 18 for SQL Server\n"
        "# Optional direct URI: MSSQL_CONNECTION_STRING=mssql+pyodbc://user:pass@127.0.0.1:1433/analytics?driver=ODBC+Driver+18+for+SQL+Server\n"
    ),
    "mssql": (
        "# Microsoft SQL Server (MSSQL) Connection Configuration\n"
        "MSSQL_HOST=127.0.0.1\n"
        "MSSQL_PORT=1433\n"
        "MSSQL_USER=readonly_user\n"
        "MSSQL_PASSWORD=your_password\n"
        "MSSQL_DATABASE=analytics\n"
        "MSSQL_DRIVER=ODBC Driver 18 for SQL Server\n"
    ),
    "snowflake": (
        "# Snowflake Connection Configuration\n"
        "SNOWFLAKE_ACCOUNT=xy12345.us-east-1\n"
        "SNOWFLAKE_USER=readonly_user\n"
        "SNOWFLAKE_PASSWORD=your_password\n"
        "SNOWFLAKE_DATABASE=analytics\n"
        "SNOWFLAKE_SCHEMA=public\n"
        "SNOWFLAKE_WAREHOUSE=compute_wh\n"
    ),
}


def _get_env_template(db: str) -> str:
    """Returns the .env template for a known or extensible database adapter."""
    db_lower = db.lower()
    if db_lower in _ENV_TEMPLATES:
        return _ENV_TEMPLATES[db_lower]
    prefix = db.upper()
    return (
        f"# Connection configuration for {db}\n"
        f"{prefix}_HOST=127.0.0.1\n"
        f"{prefix}_PORT=\n"
        f"{prefix}_USER=readonly_user\n"
        f"{prefix}_PASSWORD=your_password\n"
        f"{prefix}_DATABASE=your_database\n"
        f"# Optional direct URI/DSN format:\n"
        f"#{prefix}_DSN={db_lower}://user:pass@127.0.0.1:port/database\n"
    )


ALLOWED_LEADING = ("SELECT", "WITH")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _available_databases() -> list[str]:
    return sorted(
        p.stem[len("evaluator_"):]
        for p in TEMPLATES.glob("evaluator_*.py")
    )


def _die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def _strip_sql_comments(sql: str) -> str:
    no_block = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", " ", no_block)


def _validate_sql(sql: str) -> None:
    cleaned = _strip_sql_comments(sql).strip().rstrip(";").strip()
    if not cleaned:
        _die("SQL is empty")
    if ";" in cleaned:
        _die("SQL must be a single statement (no ';')")
    leading = cleaned.split(None, 1)[0].upper()
    if leading not in ALLOWED_LEADING:
        _die(f"SQL must start with SELECT or WITH, got '{leading}'")
    if '"""' in sql:
        _die('SQL must not contain the triple-quote sequence """')


def _replace_region(text: str, start_needle: str, end_needle: str,
                    new_inner: str) -> str:
    """Replace the lines strictly between the marker lines, keeping markers."""
    lines = text.splitlines(keepends=True)
    start = end = None
    for i, ln in enumerate(lines):
        if start is None and start_needle in ln:
            start = i
        elif start is not None and end_needle in ln:
            end = i
            break
    if start is None or end is None:
        _die(f"could not find region markers {start_needle!r}..{end_needle!r}")
    new_block = new_inner if new_inner.endswith("\n") else new_inner + "\n"
    return "".join(lines[: start + 1]) + new_block + "".join(lines[end:])


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def _import_evaluator(evaluator_path: Path):
    spec = importlib.util.spec_from_file_location("generated_evaluator", evaluator_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_list_databases(_args) -> None:
    dbs = _available_databases()
    if not dbs:
        _die(f"no database adapters found in {TEMPLATES}")
    print("Supported databases:")
    for d in dbs:
        print(f"  - {d}")


def cmd_init(args) -> None:
    dbs = _available_databases()
    if args.db not in dbs:
        _die(f"unknown database '{args.db}'. Supported: {', '.join(dbs)}")

    if args.sql_file:
        sql = Path(args.sql_file).read_text()
    elif args.sql:
        sql = args.sql
    else:
        _die("provide --sql-file or --sql")
    sql = sql.strip()
    _validate_sql(sql)

    out = Path(args.out).resolve()
    if out.exists() and any(out.iterdir()):
        _die(f"output dir {out} exists and is not empty")
    out.mkdir(parents=True, exist_ok=True)

    title = args.title or f"SQL optimization ({args.db})"
    hints = args.hints or "- (none provided)"

    # 1) evaluator.py: copy the database adapter, substitute BASELINE_SQL region
    tmpl = (TEMPLATES / f"evaluator_{args.db}.py").read_text()
    sql_block = f'BASELINE_SQL = """\n{sql}\n"""'
    evaluator = _replace_region(
        tmpl, "BASELINE_SQL_START", "BASELINE_SQL_END", sql_block)
    (out / "evaluator.py").write_text(evaluator)

    # 2) initial_program.py: seed OPTIMIZED_SQL = same query
    ip = (TEMPLATES / "initial_program.py.tmpl").read_text()
    ip = ip.replace("{{OPTIMIZED_SQL}}", sql).replace("{{DB}}", args.db)
    (out / "initial_program.py").write_text(ip)

    # 3) problem_description.md
    pd = (TEMPLATES / "problem_description.md.tmpl").read_text()
    pd = (pd.replace("{{TITLE}}", title)
            .replace("{{DB}}", args.db)
            .replace("{{OPTIMIZED_SQL}}", sql)
            .replace("{{HINTS}}", hints))
    (out / "problem_description.md").write_text(pd)

    # 4) .env.example
    (out / ".env.example").write_text(_get_env_template(args.db))

    print(f"Scaffolded {args.db} experiment in {out}")
    print("Next:")
    print(f"  1. cp {out}/.env.example {out}/.env   # then fill in credentials")
    print("  2. install the database driver in the python that runs baseline AND")
    print("     in the ae CLI tool env (see references/databases.md)")
    print(f"  3. sqlopt.py baseline --dir {out}")
    print("  4. launch with the ae CLI (see references/launch.md)")


def cmd_baseline(args) -> None:
    out = Path(args.dir).resolve()
    evaluator_path = out / "evaluator.py"
    if not evaluator_path.exists():
        _die(f"{evaluator_path} not found — run `init` first")

    env_path = out / ".env"
    example_env = out / ".env.example"
    if not env_path.exists():
        if example_env.exists():
            shutil.copy(example_env, env_path)
        print(f"Initialized configuration file at: {env_path}")

    _load_dotenv(env_path)
    mod = _import_evaluator(evaluator_path)
    if not hasattr(mod, "compute_baseline"):
        _die("generated evaluator has no compute_baseline()")

    print(f"Computing baseline against target database (timeout: {args.timeout_ms} ms)...")
    try:
        data = mod.compute_baseline(args.timeout_ms)
    except Exception as e:
        print(f"\n[ERROR] Database connection failed: {e}", file=sys.stderr)
        print(f"\nPlease verify your credentials in: {env_path}", file=sys.stderr)
        if example_env.exists():
            print("\nRequired configuration format template:", file=sys.stderr)
            print("-" * 55, file=sys.stderr)
            print(example_env.read_text().strip(), file=sys.stderr)
            print("-" * 55, file=sys.stderr)
        sys.exit(1)

    (out / "baseline.json").write_text(json.dumps(data, indent=2))

    # Patch the BASELINE_FALLBACK region so the AE sandbox (which can't see
    # baseline.json) has the fingerprint.
    sig_repr = "(\n" + "".join(
        f"        ({name!r}, {typ!r}),\n" for name, typ in data["signature"]
    ) + "    )"
    fallback = (
        f'BASELINE_HASH_FALLBACK = {data["hash"]!r}\n'
        f'BASELINE_MEDIAN_MS_FALLBACK = {data["median_ms"]!r}\n'
        f'BASELINE_SIGNATURE_FALLBACK: tuple[tuple[str, str], ...] = {sig_repr}'
    )
    text = evaluator_path.read_text()
    text = _replace_region(text, "BASELINE_FALLBACK_START",
                           "BASELINE_FALLBACK_END", fallback)
    evaluator_path.write_text(text)

    print(f"database={data['engine']}")
    print(f"hash={data['hash']}")
    print(f"median_ms={data['median_ms']:.1f}")
    print(f"signature={data['signature']}")
    print(f"Wrote {out/'baseline.json'} and patched fallback constants.")


def cmd_check(args) -> None:
    print("=== AlphaEvolve & Environment Preflight Check ===\n")
    all_ok = True

    # 1. Check gcloud CLI & Auth
    gcloud_path = shutil.which("gcloud")
    if not gcloud_path:
        print("  [✗] gcloud CLI: Not found in PATH")
        print("      -> Install: https://docs.cloud.google.com/sdk/docs/install-sdk")
        all_ok = False
    else:
        try:
            res = subprocess.run(
                ["gcloud", "auth", "print-access-token"],
                capture_output=True, text=True, timeout=10
            )
            if res.returncode == 0:
                print(f"  [✓] gcloud CLI & Auth: OK ({gcloud_path})")
            else:
                print("  [✗] gcloud Auth: Not authenticated or token expired")
                print("      -> Run: gcloud auth application-default login")
                all_ok = False
        except Exception as e:
            print(f"  [✗] gcloud Auth check failed: {e}")
            all_ok = False

    # 2. Check ae CLI
    ae_path = shutil.which("ae")
    if not ae_path:
        print("  [✗] AlphaEvolve (ae) CLI: Not found in PATH")
        print("      -> Install: pip install \"git+https://github.com/Google-Cloud-AI/alphaevolve-on-googlecloud.git#subdirectory=skills\"")
        print("      -> Or with uv: uv tool install \"git+https://github.com/Google-Cloud-AI/alphaevolve-on-googlecloud.git#subdirectory=skills\"")
        all_ok = False
    else:
        try:
            res = subprocess.run(["ae", "version"], capture_output=True, text=True, timeout=5)
            version_str = res.stdout.strip() if res.returncode == 0 else "unknown"
            print(f"  [✓] AlphaEvolve CLI: {version_str} ({ae_path})")
        except Exception:
            print(f"  [✓] AlphaEvolve CLI: Found ({ae_path})")

        # Check ae config test
        try:
            res = subprocess.run(["ae", "--json", "config", "test"], capture_output=True, text=True, timeout=15)
            if res.returncode == 0 and "error" not in res.stdout.lower():
                print("  [✓] AlphaEvolve Engine API & Licensing: OK")
            else:
                print("  [✗] AlphaEvolve Engine API Access / Licensing: FAILED")
                print(f"      Output: {res.stdout.strip() or res.stderr.strip()}")
                print("      -> Check Project ID, Engine ID, and IAM permissions.")
                print("      -> Setup Guide: https://docs.cloud.google.com/gemini/enterprise/docs/alphaevolve/developer-guide/environment-and-api-access-setup?hl=zh-cn#project-and-licensing")
                all_ok = False
        except Exception as e:
            print(f"  [✗] ae config test execution error: {e}")
            all_ok = False

    # 3. Check Database Drivers
    db_driver_map = {
        "postgres": "psycopg",
        "hologres": "psycopg",
        "clickhouse": "clickhouse_connect",
    }
    ae_py = Path.home() / ".local/share/uv/tools/ae-cli/bin/python"
    
    dbs_to_check = [args.db] if args.db else _available_databases()
    for db in dbs_to_check:
        drv = db_driver_map.get(db)
        if not drv:
            continue
        
        # Check current python
        has_local = importlib.util.find_spec(drv) is not None
        # Check ae-cli python if it exists
        has_ae = False
        if ae_py.exists():
            res = subprocess.run([str(ae_py), "-c", f"import {drv}"], capture_output=True)
            has_ae = (res.returncode == 0)
        
        if has_local and has_ae:
            print(f"  [✓] DB Driver ({db}: {drv}): OK in current Python & ae-cli env")
        else:
            status_desc = []
            if not has_local:
                status_desc.append("missing in current Python")
            if not has_ae:
                status_desc.append(f"missing in ae-cli env ({ae_py})")
            print(f"  [!] DB Driver ({db}: {drv}): {', '.join(status_desc)}")
            print(f"      -> Install command:")
            if ae_py.exists():
                pkg = "'psycopg[binary]'" if drv == "psycopg" else drv
                print(f"         uv pip install --python {ae_py} {pkg}")
            if not has_local:
                pkg = "'psycopg[binary]'" if drv == "psycopg" else drv
                print(f"         pip install {pkg}")
            if args.db and not (has_local and has_ae):
                all_ok = False

    print("\n-------------------------------------------------")
    if all_ok:
        print("Result: ALL CHECKS PASSED. Ready for optimization!")
    else:
        print("Result: SOME CHECKS FAILED. Please resolve the items above before proceeding.")
        print("Reference: https://docs.cloud.google.com/gemini/enterprise/docs/alphaevolve/developer-guide/environment-and-api-access-setup?hl=zh-cn#project-and-licensing")
        if args.db:
            sys.exit(1)


def cmd_wizard(args) -> None:
    print("=" * 70)
    print("  AlphaEvolve SQL Optimizer — Guided Experiment Orchestration Wizard")
    print("=" * 70)

    # -------------------------------------------------------------
    # Step 1: Preflight & Environment Verification
    # -------------------------------------------------------------
    print("\n[Phase 1/6] Environment & API Access Preflight Verification")
    print("Validating Google Cloud authentication, Discovery Engine API access, and database drivers...")
    cmd_check(argparse.Namespace(db=None))

    if not shutil.which("ae"):
        print("\n[!] AlphaEvolve (ae) CLI not detected in PATH.")
        print("    Select installation option:")
        print("      [1] Auto-install via pip (Recommended)")
        print("      [2] Auto-install via uv tool")
        print("      [3] Skip for now")
        ae_choice = input("\nEnter choice (1-3) [1]: ").strip() or "1"
        if ae_choice == "1":
            cmd_install_ae(argparse.Namespace(method="pip"))
        elif ae_choice == "2":
            cmd_install_ae(argparse.Namespace(method="uv"))

    # -------------------------------------------------------------
    # Step 2: Database Architecture Selection
    # -------------------------------------------------------------
    print("\n[Phase 2/6] Target Database Architecture Selection")
    dbs = _available_databases()
    for i, db in enumerate(dbs, 1):
        print(f"  [{i}] {db.upper()}")
    
    while True:
        try:
            choice = input(f"Select target engine (1-{len(dbs)}) [1]: ").strip() or "1"
            idx = int(choice) - 1
            if 0 <= idx < len(dbs):
                target_db = dbs[idx]
                break
        except (ValueError, IndexError):
            pass
        print(f"Invalid selection. Enter an integer between 1 and {len(dbs)}.")

    print(f"Selected Database Engine: {target_db.upper()}")

    # Check driver for selected engine
    drv_module = {
        "postgres": "psycopg",
        "hologres": "psycopg",
        "clickhouse": "clickhouse_connect",
        "mysql": "pymysql",
        "oracle": "oracledb",
        "sqlserver": "pyodbc",
        "mssql": "pyodbc",
        "snowflake": "snowflake",
        "duckdb": "duckdb",
        "trino": "trino",
    }.get(target_db, target_db)

    has_local = importlib.util.find_spec(drv_module) is not None
    ae_py = Path.home() / ".local/share/uv/tools/ae-cli/bin/python"
    has_ae = False
    if ae_py.exists():
        res = subprocess.run([str(ae_py), "-c", f"import {drv_module}"], capture_output=True)
        has_ae = (res.returncode == 0)

    if not (has_local and has_ae):
        print(f"\n[!] Database driver for '{target_db}' is missing or incomplete in your environment.")
        print("    Select installation option:")
        print(f"      [1] Auto-install driver '{DRIVER_PACKAGES.get(target_db, target_db)}' (Recommended)")
        print("      [2] Skip for now")
        drv_choice = input("\nEnter choice (1-2) [1]: ").strip() or "1"
        if drv_choice == "1":
            cmd_install_driver(argparse.Namespace(db=target_db, package=None))

    # -------------------------------------------------------------
    # Step 3: SQL Specification & Evolution Seed Strategy
    # -------------------------------------------------------------
    print("\n[Phase 3/6] Query Specification & Optimization Context")
    print("  [1] Load query specification from file path")
    print("  [2] Input query text interactively")
    sql_mode = input("Select query input method (1-2) [1]: ").strip() or "1"

    sql_text = ""
    if sql_mode == "1":
        while True:
            file_path = input("Enter path to SQL file: ").strip()
            p = Path(file_path).expanduser()
            if p.exists() and p.is_file():
                sql_text = p.read_text().strip()
                break
            print(f"File not found: {file_path}")
    else:
        print("Provide SQL statement (terminate input with EOF / Ctrl+D or trailing semicolon):")
        lines = []
        while True:
            try:
                line = input()
                lines.append(line)
                if line.strip().endswith(";"):
                    break
            except EOFError:
                break
        sql_text = "\n".join(lines).strip()

    _validate_sql(sql_text)
    print(f"Query validation successful: Single read-only statement verified ({len(sql_text.splitlines())} lines).")

    hints = input("\nProvide schema metadata, index structures, or cardinality estimates (optional): ").strip()
    if not hints:
        hints = "- (none provided)"

    # -------------------------------------------------------------
    # Step 4: Search Budget & Evaluation Parameters
    # -------------------------------------------------------------
    print("\n[Phase 4/6] Search Budget & Evaluation Parameters Configuration")
    print("  [1] Feasibility Verification      (10 programs, concurrency: 2, timeout: 60s)  - Rapid validation")
    print("  [2] Standard Evolutionary Search  (20 programs, concurrency: 3, timeout: 120s) - Recommended production profile")
    print("  [3] Exhaustive Global Optimization(50 programs, concurrency: 5, timeout: 300s) - Maximum search exploration")
    print("  [4] Custom Parameter Profile      (Manual specification of budget, concurrency, and timeout)")

    preset_choice = input("Select configuration profile (1-4) [2]: ").strip() or "2"
    if preset_choice == "1":
        max_programs, concurrency, timeout_sec = 10, 2, 60
    elif preset_choice == "3":
        max_programs, concurrency, timeout_sec = 50, 5, 300
    elif preset_choice == "4":
        max_programs = int(input("Candidate program budget [20]: ").strip() or "20")
        concurrency = int(input("Execution concurrency [3]: ").strip() or "3")
        timeout_sec = int(input("Per-candidate evaluation timeout in seconds [120]: ").strip() or "120")
    else:
        max_programs, concurrency, timeout_sec = 20, 3, 120

    # -------------------------------------------------------------
    # Step 5: Scaffolding & 4-Gate Baseline Measurement
    # -------------------------------------------------------------
    print("\n[Phase 5/6] Scaffolding Experiment & Computing 4-Gate Baseline")
    out_dir = Path(f"./exp_{target_db}").resolve()
    if out_dir.exists():
        counter = 1
        while (Path(f"./exp_{target_db}_{counter}").exists()):
            counter += 1
        out_dir = Path(f"./exp_{target_db}_{counter}").resolve()

    cmd_init(argparse.Namespace(
        db=target_db,
        sql=sql_text,
        sql_file=None,
        out=str(out_dir),
        title=f"SQL optimization ({target_db})",
        hints=hints
    ))

    print(f"Experiment workspace initialized: {out_dir}")
    print(f"Verify database connection parameters in {out_dir}/.env before proceeding.")
    input("Press Enter to execute baseline measurement against the target database...")

    cmd_baseline(argparse.Namespace(
        dir=str(out_dir),
        timeout_ms=timeout_sec * 1000
    ))

    # -------------------------------------------------------------
    # Step 6: Specification Review & Execution Dispatch
    # -------------------------------------------------------------
    baseline_file = out_dir / "baseline.json"
    baseline_data = json.loads(baseline_file.read_text()) if baseline_file.exists() else {}

    print("\n" + "=" * 70)
    print("  [Phase 6/6] Experiment Specification Review & Execution Dispatch")
    print("=" * 70)
    print(f"  Target Engine:           {target_db.upper()}")
    print(f"  Experiment Directory:    {out_dir}")
    print(f"  Baseline Median Runtime: {baseline_data.get('median_ms', 'N/A')} ms")
    print(f"  Result Hash Fingerprint: {baseline_data.get('hash', 'N/A')}")
    print(f"  Candidate Program Budget:{max_programs} programs (Concurrency: {concurrency})")
    print(f"  Per-Candidate Timeout:   {timeout_sec}s")
    print(f"  Generative Model:        gemini-3.5-flash")
    print("=" * 70)
    print("\nConfirm execution dispatch to AlphaEvolve:")
    print("  [1] Confirm & Dispatch Execution (Launch AlphaEvolve Optimization Loop)")
    print("  [2] Export Execution Commands (Print 'ae' CLI invocation for manual execution)")
    print("  [3] Terminate Session")

    final_choice = input("\nEnter selection (1-3) [1]: ").strip() or "1"
    if final_choice == "1":
        print("\nDispatching AlphaEvolve experiment lifecycle...")
        print("1. Creating experiment...")
        res = subprocess.run([
            "ae", "--json", "experiment", "create",
            "--max-programs", str(max_programs),
            "--concurrency", str(concurrency),
            "--problem-file", str(out_dir / "problem_description.md"),
            "--title", f"SQL optimization ({target_db})",
            "--models", "gemini-3.5-flash"
        ], capture_output=True, text=True)
        if res.returncode != 0:
            _die(f"ae experiment create failed: {res.stderr or res.stdout}")
        
        try:
            exp_info = json.loads(res.stdout)
            nickname = exp_info.get("nickname") or exp_info.get("name", "exp")
        except Exception:
            nickname = "exp"

        print(f"2. Initializing seed program for experiment '{nickname}'...")
        ae_py = Path.home() / ".local/share/uv/tools/ae-cli/bin/python"
        py_bin = str(ae_py) if ae_py.exists() else sys.executable
        seed_cmd = f"from evaluator import evaluate_program; print(evaluate_program(open('initial_program.py').read(), {timeout_sec})['score'])"
        seed_res = subprocess.run([py_bin, "-c", seed_cmd], cwd=str(out_dir), capture_output=True, text=True)
        seed_score = seed_res.stdout.strip() or "1.0"

        subprocess.run([
            "ae", "--json", "experiment", "start", nickname,
            "--program-dir", str(out_dir),
            "--score", seed_score
        ], check=True)

        print(f"3. Initiating evolutionary search loop for '{nickname}'...")
        subprocess.run([
            "ae", "--json", "experiment", "run", nickname,
            "--evaluator", str(out_dir / "evaluator.py"),
            "--backend", "local",
            "--dashboard", f"{nickname}-dashboard.md",
            "--timeout", str(timeout_sec)
        ])
    elif final_choice == "2":
        print("\nManual execution command sequence:")
        print(f"  ae --json experiment create --max-programs {max_programs} --concurrency {concurrency} --problem-file {out_dir}/problem_description.md --title 'SQL optimization' --models gemini-3.5-flash")
        print(f"  ae --json experiment start <nickname> --program-dir {out_dir} --score 1.0")
        print(f"  ae --json experiment run <nickname> --evaluator {out_dir}/evaluator.py --backend local --timeout {timeout_sec}")
    else:
        print("\nOptimization session terminated by user.")


DRIVER_PACKAGES = {
    "postgres": "psycopg[binary]",
    "hologres": "psycopg[binary]",
    "clickhouse": "clickhouse-connect",
    "mysql": "pymysql",
    "oracle": "oracledb",
    "sqlserver": "pyodbc",
    "mssql": "pyodbc",
    "snowflake": "snowflake-connector-python",
    "duckdb": "duckdb",
    "sqlite": None,
    "trino": "trino",
    "starrocks": "pymysql",
}


def cmd_install_driver(args) -> None:
    db = args.db.lower()
    pkg = DRIVER_PACKAGES.get(db)
    if db == "sqlite":
        print("SQLite driver ('sqlite3') is included in the Python standard library.")
        return
    if not pkg:
        pkg = args.package or db

    print(f"Provisioning database driver '{pkg}' for engine '{db}'...")
    ae_py = Path.home() / ".local/share/uv/tools/ae-cli/bin/python"

    # Install in AlphaEvolve CLI environment
    if ae_py.exists():
        subprocess.run(["uv", "pip", "install", "--python", str(ae_py), pkg], check=True)
        print(f"Driver '{pkg}' installed into AlphaEvolve environment ({ae_py}).")

    # Install in active Python environment
    subprocess.run([sys.executable, "-m", "pip", "install", pkg], check=True)
    print(f"Driver '{pkg}' installed into active Python environment ({sys.executable}).")


AE_GIT_URL = "git+https://github.com/Google-Cloud-AI/alphaevolve-on-googlecloud.git#subdirectory=skills"


def cmd_install_ae(args) -> None:
    method = (args.method or "pip").lower()
    print(f"Installing AlphaEvolve (ae) CLI via {method}...")
    if method == "uv":
        res = subprocess.run(["uv", "tool", "install", "--force", "--default-index", "https://pypi.org/simple", AE_GIT_URL])
    else:
        res = subprocess.run([sys.executable, "-m", "pip", "install", "--index-url", "https://pypi.org/simple", AE_GIT_URL])

    if res.returncode == 0:
        print("AlphaEvolve CLI installation completed successfully.")
        ae_path = shutil.which("ae")
        if ae_path:
            ver = subprocess.run(["ae", "version"], capture_output=True, text=True)
            print(f"Verified: {ver.stdout.strip()} ({ae_path})")
    else:
        print("Installation encountered an error. Please review the output above.", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(prog="sqlopt", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ae = sub.add_parser("install-ae", help="auto-install AlphaEvolve (ae) CLI")
    p_ae.add_argument("--method", choices=["pip", "uv"], default="pip", help="installation tool (pip or uv)")
    p_ae.set_defaults(func=cmd_install_ae)

    p_driver = sub.add_parser("install-driver", help="auto-provision driver for a database engine")
    p_driver.add_argument("db", help="database engine name (e.g. postgres, clickhouse, mysql, oracle, mssql)")
    p_driver.add_argument("--package", help="optional custom PyPI package name override")
    p_driver.set_defaults(func=cmd_install_driver)

    p_wizard = sub.add_parser("wizard", help="interactive step-by-step optimization wizard")
    p_wizard.set_defaults(func=cmd_wizard)

    p_check = sub.add_parser("check", help="check AE permissions, engine, and DB drivers")
    p_check.add_argument("--db", help="optional database name to check specific driver (e.g. postgres, clickhouse)")
    p_check.set_defaults(func=cmd_check)

    p_init = sub.add_parser("init", help="scaffold an experiment directory")
    p_init.add_argument("--db", required=True, help="database name (see list-databases)")
    p_init.add_argument("--sql-file")
    p_init.add_argument("--sql")
    p_init.add_argument("--out", required=True)
    p_init.add_argument("--title")
    p_init.add_argument("--hints")
    p_init.set_defaults(func=cmd_init)

    p_base = sub.add_parser("baseline", help="compute + cache the baseline")
    p_base.add_argument("--dir", required=True)
    p_base.add_argument("--timeout-ms", type=int, default=120_000)
    p_base.set_defaults(func=cmd_baseline)

    p_list = sub.add_parser("list-databases", help="list supported databases")
    p_list.set_defaults(func=cmd_list_databases)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
