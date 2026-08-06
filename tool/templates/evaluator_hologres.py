"""AlphaEvolve evaluator — Hologres engine.

Hologres speaks the PostgreSQL wire protocol, so `psycopg` connects directly.
Differences from the vanilla PostgreSQL template are confined to the five
ENGINE-SPECIFIC spots — chiefly L4 timing, because Hologres' EXPLAIN ANALYZE
output differs from PostgreSQL and the HQE (native) vs PQE (Postgres-compat)
execution engine choice materially changes latency.

NOTE: this template is validated against the PG-compatible surface only; final
sign-off of the EXPLAIN parsing requires a real Hologres instance. See
references/databases.md.

Contract for the `ae` CLI:
    python evaluator.py --output-file <path> --program-dir <dir>
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import signal
import statistics
import time
import traceback
from pathlib import Path
from typing import Any

# ENGINE-SPECIFIC (1a): driver import  (Hologres = PG wire protocol)
import psycopg

ENGINE = "hologres"

# === BASELINE_SQL_START ===  (substituted by `sqlopt.py init`)
BASELINE_SQL = """
SELECT 1 AS placeholder
"""
# === BASELINE_SQL_END ===

# === BASELINE_FALLBACK_START ===  (substituted by `sqlopt.py baseline`)
BASELINE_HASH_FALLBACK = "__RECOMPUTE_ME__"
BASELINE_MEDIAN_MS_FALLBACK = 0.0
BASELINE_SIGNATURE_FALLBACK: tuple[tuple[str, str], ...] = ()
# === BASELINE_FALLBACK_END ===


# ENGINE-SPECIFIC (1b): connection (libpq env vars; Hologres endpoint:port)
def _connect() -> "psycopg.Connection":
    return psycopg.connect(
        host=os.getenv("PGHOST", "127.0.0.1"),
        port=int(os.getenv("PGPORT", "80")),
        user=os.getenv("PGUSER", ""),
        password=os.getenv("PGPASSWORD", ""),
        dbname=os.getenv("PGDATABASE", "postgres"),
        autocommit=True,
    )


# ENGINE-SPECIFIC (2): read-only (best-effort; L1 gate already blocks writes)
def _set_readonly(conn) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute("SET SESSION default_transaction_read_only = on")
    except Exception:  # noqa: BLE001 - not all Hologres versions accept this
        pass


# ENGINE-SPECIFIC (3): L2 column signature (same as PG)
def _get_column_signature(conn, sql: str, timeout_ms: int) -> tuple[tuple[str, str], ...]:
    with conn.cursor() as cur:
        with contextlib.suppress(Exception):
            cur.execute(f"SET statement_timeout = {int(timeout_ms)}")
        cur.execute(f"SELECT * FROM ({sql}) AS t LIMIT 0")
        cols = list(cur.description)
        if not cols:
            return tuple()
        oids = list({c.type_code for c in cols})
        cur.execute("SELECT oid, typname FROM pg_type WHERE oid = ANY(%s)", (oids,))
        name_by_oid = {oid: name for oid, name in cur.fetchall()}
    return tuple(
        (c.name, name_by_oid.get(c.type_code, f"oid_{c.type_code}")) for c in cols
    )


# ENGINE-SPECIFIC (4): L3 order-agnostic result hash
# Hologres supports jsonb + string_agg in recent versions. If your instance
# rejects to_jsonb, swap the inner SELECT for an explicit column concat (see
# references/databases.md).
def _hash_result_set(conn, sql: str, timeout_ms: int) -> str:
    wrapped = f"""
        SELECT md5(string_agg(row_data, ',' ORDER BY row_data))
          FROM (
            SELECT (to_jsonb(t.*))::text AS row_data
              FROM ({sql}) AS t
          ) sub
    """
    with conn.cursor() as cur:
        with contextlib.suppress(Exception):
            cur.execute(f"SET statement_timeout = {int(timeout_ms)}")
        cur.execute(wrapped)
        row = cur.fetchone()
    return row[0] if row and row[0] is not None else "<empty-result-set>"


# ENGINE-SPECIFIC (5): L4 timing — defensive across Hologres versions.
#   a) try EXPLAIN (ANALYZE, FORMAT JSON) → "Execution Time"
#   b) else EXPLAIN ANALYZE text → parse a total-time line
#   c) else client wall-clock around a fully-fetched execution
# Also records the execution engine (HQE/PQE) into _LAST_ENGINE_NOTE.
_LAST_ENGINE_NOTE = ""


def _measure_execution_ms(conn, sql: str, timeout_ms: int) -> float:
    global _LAST_ENGINE_NOTE
    with conn.cursor() as cur:
        with contextlib.suppress(Exception):
            cur.execute(f"SET statement_timeout = {int(timeout_ms)}")

        # (a) JSON plan
        try:
            cur.execute(f"EXPLAIN (ANALYZE, VERBOSE, FORMAT JSON) {sql}")
            plan = cur.fetchone()[0]
            if isinstance(plan, str):
                plan = json.loads(plan)
            node = plan[0] if isinstance(plan, list) else plan
            if isinstance(node, dict) and "Execution Time" in node:
                return float(node["Execution Time"])
        except Exception:  # noqa: BLE001
            pass

        # (b) text plan
        try:
            cur.execute(f"EXPLAIN ANALYZE {sql}")
            text = "\n".join(str(r[0]) for r in cur.fetchall())
            if "HQE" in text:
                _LAST_ENGINE_NOTE = "exec_engine=HQE"
            elif "PQE" in text or "Postgres" in text:
                _LAST_ENGINE_NOTE = "exec_engine=PQE"
            m = re.search(r"Execution Time:\s*([\d.]+)\s*ms", text)
            if m:
                return float(m.group(1))
            m = re.search(r"total[_ ]?time[=:]\s*([\d.]+)\s*ms", text, re.IGNORECASE)
            if m:
                return float(m.group(1))
        except Exception:  # noqa: BLE001
            pass

    # (c) client wall-clock fallback
    t0 = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(sql)
        cur.fetchall()
    _LAST_ENGINE_NOTE = (_LAST_ENGINE_NOTE + " timing=client_wallclock").strip()
    return (time.perf_counter() - t0) * 1000.0


# ===================================================================== #
# Fixed skeleton below — identical across all engine templates
# ===================================================================== #

FAILURE_SCORE = -1e12
NUM_TIMED_RUNS = 3
BASELINE_FILE = "baseline.json"

ALLOWED_LEADING = ("SELECT", "WITH")
FORBIDDEN_TOKENS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|"
    r"TRUNCATE|GRANT|REVOKE|COPY|VACUUM|ANALYZE|"
    r"NOTIFY|LISTEN|LOCK|SET|RESET|SHOW|ATTACH|DETACH|OPTIMIZE)\b",
    re.IGNORECASE,
)


class EvaluationTimeoutError(Exception):
    pass


def _timeout_handler(signum, frame):
    raise EvaluationTimeoutError("Evaluation timed out")


if hasattr(signal, "SIGALRM"):
    signal.signal(signal.SIGALRM, _timeout_handler)


def _strip_sql_comments(sql: str) -> str:
    no_block = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", " ", no_block)


def _extract_and_validate(
    code: str, stdout: io.StringIO, stderr: io.StringIO
) -> tuple[str | None, dict[str, Any] | None]:
    ns: dict[str, Any] = {}
    try:
        with (contextlib.redirect_stdout(stdout),
              contextlib.redirect_stderr(stderr)):
            exec(code, ns)  # noqa: S102
    except Exception as e:  # noqa: BLE001
        return None, _fail(
            f"Candidate program failed to import: {e}",
            tb=traceback.format_exc(),
            stdout=stdout.getvalue(), stderr=stderr.getvalue())

    sql = ns.get("OPTIMIZED_SQL")
    if not isinstance(sql, str) or not sql.strip():
        return None, _fail(
            "Candidate did not define a non-empty OPTIMIZED_SQL string",
            stdout=stdout.getvalue(), stderr=stderr.getvalue())

    cleaned = _strip_sql_comments(sql).strip().rstrip(";").strip()
    if not cleaned:
        return None, _fail("OPTIMIZED_SQL was blank after comment stripping")
    if ";" in cleaned:
        return None, _fail("OPTIMIZED_SQL must be a single statement (no ';')")

    leading = cleaned.split(None, 1)[0].upper()
    if leading not in ALLOWED_LEADING:
        return None, _fail(
            f"OPTIMIZED_SQL must start with SELECT or WITH, got '{leading}'")
    if FORBIDDEN_TOKENS.search(cleaned):
        return None, _fail("OPTIMIZED_SQL contains a forbidden keyword")

    return sql, None


def _load_baseline() -> tuple[str, float, tuple[tuple[str, str], ...]]:
    path = Path(__file__).parent / BASELINE_FILE
    if path.exists():
        raw = json.loads(path.read_text())
        return (raw["hash"], float(raw["median_ms"]),
                tuple(tuple(x) for x in raw.get("signature", ())))
    return (BASELINE_HASH_FALLBACK, BASELINE_MEDIAN_MS_FALLBACK,
            BASELINE_SIGNATURE_FALLBACK)


def compute_baseline(timeout_ms: int = 120_000) -> dict[str, Any]:
    with _connect() as conn:
        _set_readonly(conn)
        sig = _get_column_signature(conn, BASELINE_SQL, timeout_ms)
        digest = _hash_result_set(conn, BASELINE_SQL, timeout_ms)
        runs = [_measure_execution_ms(conn, BASELINE_SQL, timeout_ms)
                for _ in range(NUM_TIMED_RUNS)]
    return {
        "engine": ENGINE,
        "hash": digest,
        "median_ms": statistics.median(runs),
        "signature": [list(x) for x in sig],
        "runs_ms": runs,
    }


def evaluate_program(code: str, timeout_seconds: int = 30) -> dict[str, Any]:
    stdout, stderr = io.StringIO(), io.StringIO()
    stmt_timeout_ms = max(1_000, int(timeout_seconds * 1_000))

    try:
        if hasattr(signal, "alarm"):
            signal.alarm(timeout_seconds + 5)

        candidate_sql, failure = _extract_and_validate(code, stdout, stderr)
        if failure is not None:
            return failure

        base_hash, base_median_ms, base_sig = _load_baseline()

        with _connect() as conn:
            _set_readonly(conn)

            try:
                cand_sig = _get_column_signature(conn, candidate_sql, stmt_timeout_ms)
            except Exception as e:  # noqa: BLE001
                return _fail(f"Candidate SQL failed schema check: {e}",
                             tb=traceback.format_exc(),
                             stdout=stdout.getvalue(), stderr=stderr.getvalue())
            if cand_sig != base_sig:
                return _fail(
                    f"Column signature mismatch: baseline={base_sig} candidate={cand_sig}",
                    stdout=stdout.getvalue(), stderr=stderr.getvalue())

            try:
                cand_hash = _hash_result_set(conn, candidate_sql, stmt_timeout_ms)
            except Exception as e:  # noqa: BLE001
                return _fail(f"Candidate SQL failed to execute: {e}",
                             tb=traceback.format_exc(),
                             stdout=stdout.getvalue(), stderr=stderr.getvalue())
            if cand_hash != base_hash:
                return _fail(
                    f"Candidate result set does not match baseline "
                    f"(baseline={base_hash}, candidate={cand_hash})",
                    stdout=stdout.getvalue(), stderr=stderr.getvalue())

            timings: list[float] = []
            for _ in range(NUM_TIMED_RUNS):
                try:
                    timings.append(
                        _measure_execution_ms(conn, candidate_sql, stmt_timeout_ms))
                except Exception as e:  # noqa: BLE001
                    return _fail(f"Candidate SQL failed during timed run: {e}",
                                 tb=traceback.format_exc(),
                                 stdout=stdout.getvalue(), stderr=stderr.getvalue())

        cand_median = statistics.median(timings)
        speedup = base_median_ms / cand_median if cand_median > 0 else FAILURE_SCORE

        timing_text = (
            f"engine={ENGINE} baseline_median_ms={base_median_ms:.2f} "
            f"candidate_median_ms={cand_median:.2f} speedup={speedup:.3f}x "
            f"runs_ms={[round(t, 2) for t in timings]}")
        if _LAST_ENGINE_NOTE:
            timing_text += f" {_LAST_ENGINE_NOTE}"

        insights = [
            {"label": "timing", "text": timing_text},
            {"label": "correctness",
             "text": f"result_hash_matches_baseline hash={cand_hash}"},
        ]
        if stdout.getvalue():
            insights.append({"label": "stdout", "text": stdout.getvalue()})
        if stderr.getvalue():
            insights.append({"label": "stderr", "text": stderr.getvalue()})
        return {"score": float(speedup), "insights": insights}

    except EvaluationTimeoutError:
        return _fail(f"Evaluation timed out after {timeout_seconds}s",
                     tb=traceback.format_exc(),
                     stdout=stdout.getvalue(), stderr=stderr.getvalue())
    except Exception as e:  # noqa: BLE001
        return _fail(f"Evaluation failed: {e}",
                     tb=traceback.format_exc(),
                     stdout=stdout.getvalue(), stderr=stderr.getvalue())
    finally:
        if hasattr(signal, "alarm"):
            signal.alarm(0)


def _fail(error: str, tb: str | None = None, stdout: str = "", stderr: str = "") -> dict[str, Any]:
    ins: list[dict[str, str]] = [{"label": "error", "text": error}]
    if tb:
        ins.append({"label": "traceback", "text": tb})
    if stdout:
        ins.append({"label": "stdout", "text": stdout})
    if stderr:
        ins.append({"label": "stderr", "text": stderr})
    return {"score": FAILURE_SCORE, "insights": ins}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-file", required=True)
    parser.add_argument("--program-dir", default=None)
    parser.add_argument("--input-program-file", action="append", default=None)
    args, _ = parser.parse_known_args()

    if args.program_dir:
        program_path = Path(args.program_dir) / "initial_program.py"
    elif args.input_program_file:
        program_path = Path(args.input_program_file[0])
    else:
        parser.error("one of --program-dir or --input-program-file is required")

    result = evaluate_program(program_path.read_text())
    with open(args.output_file, "w") as f:
        json.dump(result, f)


if __name__ == "__main__":
    main()
