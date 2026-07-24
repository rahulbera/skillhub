# ChampSim reference backend (`cluster_run.py`)

The ChampSim orchestrator `champsim-infra/scripts/cluster_run.py` is the
reference implementation of the `slurm-launch` backend contract. It launches
ChampSim-based simulators (Hermes / Pythia / arishem / …) on an SSH-only Slurm
cluster. The human runbook is `cluster-run.md` in this directory.

Invoke as: `python3.12 <infra>/scripts/cluster_run.py <verb> --repo <repo> …`

## Verb mapping

| Contract verb | `cluster_run.py` subcommand | ChampSim specifics |
|---|---|---|
| `configure` | `bootstrap` | Asks for remote sim path + build command; derives `remote_base`/`remote_infra_path`/`remote_runs_base`, `remote_python`, slurm defaults. Writes `config.yml`, adds `.cluster-run/` to `.git/info/exclude`, SSH-pings the cluster. `--force` re-bootstraps. |
| `submit` | `submit` | Job spec = trace list(s) `--tlist`, experiment file(s) `--exp`, metric file(s) `--mfile` (paths inside must be cluster-valid NFS paths). One command: rsyncs the sim **and** champsim-infra, builds over SSH, runs `create_jobfile.py --no-trace-cache --smoke-test-auto-launch`. Smoke sim gates the batch; on pass, all sbatch jobs submit with `tag→job_id` captured. |
| `status` | `status` | `squeue` (+ `sacct` for finished), updates each ledger, prints per-state counts; batch → `[complete]` when all jobs terminal. |
| `collect` | `rollup` | Runs `rollup.py` on the cluster over the `.out/.err` on NFS, fetches `stats.csv` to `<repo>/.cluster-run/runs/<id>/stats.csv`, flags failed/filtered runs, diffs vs the previous rolled-up batch. Refuses incomplete unless `--force`. |
| `combine` | `combine` | Merges several complete batches into one `rollup.py -d <dirA> <dirB> …` pass (incremental experiments). `trace_failed` applies across batches; writes no ledger, no compare. |

## ChampSim-specific realizations of the contract's cross-cutting rules

- **Snapshotting** — `create_jobfile.py` hardlinks the freshly-built binary into
  the batch run dir (`<run-dir>/bin/<exe>.<ts>`) and points that batch's jobs at
  the snapshot, so a later submit's rebuild can't change a prior batch. This is
  why ChampSim batches are independent / parallel-safe.
- **Smoke-gate** — `--smoke-warmup/--smoke-sim` tune the gate; `--smoke-idx`
  picks which (trace×exp) pair smoke-tests.
- **Error ids** — `submit` prints stable ids: `CJ_SMOKE_FAILED` (with output
  tail), `CJ_EXE_NOT_FOUND`, `CJ_DUPLICATE_NAME`, build failure.
- **Staleness trap** — only `submit` rsyncs champsim-infra; if you edit a
  remote-executed script (`rollup.py`, `create_jobfile.py`) then run
  `rollup`/`combine` without an intervening `submit`, the cluster runs the stale
  copy (symptom: `rollup.py: error: unrecognized arguments`). Fix:
  `rsync champsim-infra/scripts/<file>.py <cluster>:<remote_infra_path>/scripts/`.

## kratos2 facts (a configured cluster)
- Partition is **`cpu_part`** (not `compute`).
- Remote python is **`python3.10`** (needs ≥3.9 for `argparse.BooleanOptionalAction` + pyyaml), not `python3.12`.
- Poll with `squeue`; failures surface from the run outputs.
