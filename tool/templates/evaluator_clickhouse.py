"""AlphaEvolve evaluator — ClickHouse engine (incl. Alibaba Cloud ClickHouse).

Self-contained. Uses the pure-Python `clickhouse-connect` driver. The five
ENGINE-SPECIFIC spots differ from the PostgreSQL template; the skeleton is the
same shape.

Correctness (L3) uses an order-agnostic multiset fingerprint:
    count() : sum(cityHash64(*))
sum() is commutative so row order does not matter; count() guards row-count
changes. Column names/types are covered separately by L2 (DESCRIBE).

Timing (L4) reads server-side `query_duration_ms` from `system.query_log`
(needs `SYSTEM FLUSH LOGS`); falls back to client wall-clock if the account
cannot read the log.

Contract for the `ae` CLI:
    python evaluator.py --output-file <path> --program-dir <dir>
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import os
import re
import signal
import statistics
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

# ENGINE-SPECIFIC (1a): driver import
import clickhouse_connect

ENGINE = "clickhouse"

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


def _ro(timeout_ms: int) -> dict[str, Any]:
    """Per-query settings: read-only + honor the timeout."""
    return {"readonly": 2, "max_execution_time": max(1, math.ceil(timeout_ms / 1000))}


# ENGINE-SPECIFIC (1b): connection (context manager so the skeleton's
# `with _connect() as conn:` works uniformly).
@contextlib.contextmanager
def _connect():
    client = clickhouse_connect.get_client(
        host=os.getenv("CH_HOST", "127.0.0.1"),
        port=int(os.getenv("CH_PORT", "8123")),
        username=os.getenv("CH_USER", "default"),
        password=os.getenv("CH_PASSWORD", ""),
        database=os.getenv("CH_DATABASE", "default"),
        secure=os.getenv("CH_SECURE", "false").lower() in ("1", "true", "yes"),
    )
    try:
        yield client
    finally:
        client.close()


# ENGINE-SPECIFIC (2): read-only is applied per-query via _ro(); nothing to do here.
def _set_readonly(conn) -> None:
    return None


# ENGINE-SPECIFIC (3): L2 column signature via DESCRIBE (no data scan)
def _get_column_signature(conn, sql: str, timeout_ms: int) -> tuple[tuple[str, str], ...]:
    res = conn.query(f"DESCRIBE ({sql})", settings=_ro(timeout_ms))
    # DESCRIBE rows: (name, type, default_type, ...)
    return tuple((str(r[0]), str(r[1])) for r in res.result_rows)


# ENGINE-SPECIFIC (4): L3 order-agnostic multiset fingerprint
def _hash_result_set(conn, sql: str, timeout_ms: int) -> str:
    q = f"SELECT count() AS c, sum(cityHash64(*)) AS h FROM ({sql})"
    res = conn.query(q, settings=_ro(timeout_ms))
    if not res.result_rows:
        return "<empty-result-set>"
    c, h = res.result_rows[0]
    return f"{c}:{h}"


# ENGINE-SPECIFIC (5): L4 server-side timing from system.query_log
def _measure_execution_ms(conn, sql: str, timeout_ms: int) -> float:
    qid = "sqlopt_" + uuid.uuid4().hex
    settings = _ro(timeout_ms)
    # clickhouse-connect passes query_id through settings, not as a kwarg.
    conn.query(sql, settings={**settings, "query_id": qid})  # server executes fully
    try:
        conn.command("SYSTEM FLUSH LOGS")
        res = conn.query(
            "SELECT query_duration_ms FROM system.query_log "
            f"WHERE query_id = '{qid}' AND type = 'QueryFinish' "
            "ORDER BY event_time DESC LIMIT 1"
        )
        if res.result_rows:
            return float(res.result_rows[0][0])
    except Exception:  # noqa: BLE001 - readonly account may not read the log
        pass
    # Fallback: client wall-clock (includes network; least preferred).
    t0 = time.perf_counter()
    conn.query(sql, settings=settings)
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

        insights = [
            {"label": "timing", "text": (
                f"engine={ENGINE} baseline_median_ms={base_median_ms:.2f} "
                f"candidate_median_ms={cand_median:.2f} speedup={speedup:.3f}x "
                f"runs_ms={[round(t, 2) for t in timings]}")},
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
