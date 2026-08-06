# Troubleshooting

## AlphaEvolve API & Permission Errors (`ae config test` fails)

If `ae --json config test` fails or reports authentication / permission issues:

1. **Re-authenticate Google Cloud**:
   ```bash
   gcloud auth application-default login
   ```
2. **Verify Project Licensing & IAM**:
   - Ensure the GCP project has Gemini Enterprise enabled.
   - Verify the `discoveryengine.googleapis.com` API is enabled:
     ```bash
     gcloud services enable discoveryengine.googleapis.com
     ```
   - Ensure the user or Service Account has `roles/discoveryengine.admin` and `roles/iam.serviceAccountTokenCreator`.
   - See official setup guide: [AlphaEvolve Environment & API Setup Guide](https://docs.cloud.google.com/gemini/enterprise/docs/alphaevolve/developer-guide/environment-and-api-access-setup?hl=zh-cn#project-and-licensing).
3. **Check `ae` configuration**:
   ```bash
   ae config --project=<PROJECT_ID> --engine=<ENGINE_ID> --location=global --models=gemini-3.5-flash
   ae --json config test
   ```

## `ModuleNotFoundError: No module named 'psycopg'` / `clickhouse_connect`

The database driver is missing from the `ae` CLI tool env, where the evaluator
runs. Install it there:

```bash
uv pip install --python ~/.local/share/uv/tools/ae-cli/bin/python <driver>
```

`<driver>` = `'psycopg[binary]'` (postgres/hologres) or `clickhouse-connect`.
Re-running the `ae` CLI install with `--force` recreates this env and removes the
driver, so reinstall it afterward.

## `sqlopt.py baseline` can't connect

- Confirm `<exp_dir>/.env` exists (not just `.env.example`) and is filled.
- Test the connection independently (`psql` / `clickhouse-client` / curl the CH
  HTTP port).
- Network path: the machine running `baseline` and the `ae` run loop must reach
  the DB directly. High RTT inflates and destabilizes timings.

## Seed score is not ≈ 1.0

The seed is the baseline query, so it should score ~1.0. If it doesn't:
- `baseline.json` may be stale — `rm <exp_dir>/baseline.json` and re-run
  `sqlopt.py baseline`.
- The DB data changed between baseline and evaluation. Re-baseline.

## Every candidate scores `-1e12`

Read the `error` insight in `ae --json program list <nickname>`:
- `ModuleNotFoundError` → driver not in the ae tool env (see above).
- `Column signature mismatch` → the LLM is changing columns/types; the seed and
  baseline may disagree — re-check that `init` used the same query for both.
- `does not match baseline` → candidates genuinely return different rows (normal
  for bad candidates; a problem only if *all* do — then the baseline hash may be
  stale, re-baseline).
- `timed out` → raise `--timeout`, or the baseline query is too slow for the cap.

## Empty leaderboard / no candidates evaluated

`No programs available, waiting…` for a long time usually means the backend is
still generating, or quota is exhausted. Check `ae --json experiment describe
<nickname>` and the GCP quota. A too-terse `problem_description.md` also starves
the LLM — add schema and hints via `sqlopt.py init --hints`.

## ClickHouse timing looks like client wall-clock

If the timing insight shows large/variable ms, the account probably can't read
`system.query_log` (so it fell back to wall-clock). Grant read on
`system.query_log` or accept the wall-clock number (it includes network RTT —
keep the run loop close to the DB).

## Hologres: `to_jsonb` not found / EXPLAIN parse fails

- Swap the `_hash_result_set` inner select for an explicit
  `concat_ws('|', col::text, ...)` (see `references/databases.md`).
- If timing falls back to wall-clock, that's expected on some versions; for a
  fair server-side number, validate the EXPLAIN parsing against your instance.

## Reward hacking

If a top candidate looks too good, inspect its `OPTIMIZED_SQL`. The gates block
the common exploits (constant/empty/LIMIT results, DDL/DML, column changes), but
always have a human confirm the rewrite is a genuine plan improvement before
production.
