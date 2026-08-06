# Launching the experiment (ae CLI)

`sqlopt.py` scaffolds and baselines; the AlphaEvolve run itself is the standard
three-step `ae` flow. Run these from anywhere; paths point at the experiment dir.

## Prereqs

- `ae --json config test` returns success.
- The database driver is installed in the `ae` CLI tool env (see
  `references/databases.md`).
- `<exp_dir>/baseline.json` exists (from `sqlopt.py baseline`).

## Seed score

The baseline query and the seed `OPTIMIZED_SQL` are identical, so the seed score
is ≈ 1.0. Compute it for `--score`:

```bash
AE_PY=~/.local/share/uv/tools/ae-cli/bin/python
SEED=$(cd <exp_dir> && $AE_PY -c "from evaluator import evaluate_program; \
  print(evaluate_program(open('initial_program.py').read(), 90)['score'])")
```

## Three steps

```bash
# 1) create — returns a nickname
ae --json experiment create \
    --max-programs 20 \
    --concurrency 3 \
    --problem-file <exp_dir>/problem_description.md \
    --title "<short title>" \
    --models gemini-3.5-flash

# 2) start — upload the seed + baseline score (nickname from step 1)
ae --json experiment start <nickname> \
    --program-dir <exp_dir> \
    --score "$SEED"

# 3) run — the local acquire→evaluate→submit loop (blocking)
ae --json experiment run <nickname> \
    --evaluator <exp_dir>/evaluator.py \
    --backend local \
    --dashboard <nickname>-dashboard.md \
    --timeout 120
```

Notes:
- `--program-dir` uploads only `initial_program.py` as the evolving program;
  `evaluator.py` is passed separately in step 3. Keep the experiment dir clean
  (no extra `.py` files) so nothing else is bundled as program context.
- Start with `--max-programs 10–20` to confirm the loop is green (candidates
  succeeding, not all `-1e12`), then relaunch with a larger budget.
- `--timeout` is the per-candidate cap in seconds — set it above the baseline's
  own runtime.
- To run unattended, background step 3 and tail the dashboard file.

## Reading results

```bash
ae --json program list <nickname> --order-by "score desc"
```

The top program's `content.files[0].content` holds the evolved
`initial_program.py`; its `OPTIMIZED_SQL` (inside the EVOLVE-BLOCK) is the
rewrite. The `evaluation.insights` carry the `timing` (speedup) and
`correctness` (hash match) lines.
