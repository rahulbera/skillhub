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
| `submit` | `submit` | Job spec = trace list(s) `--tlist`, experiment file(s) `--exp`, metric file(s) `--mfile` (paths inside must be cluster-valid NFS paths). One command, one at a time per checkout: claims the batch id, stages the inputs (refusing an exp that still reads the live sim tree), rsyncs the sim **and** champsim-infra, builds over SSH, copies the sim's `snapshot_dirs` (default `config/`) and infra `scripts/` into the run dir, then runs that copy's `create_jobfile.py --no-trace-cache --smoke-test-auto-launch`. Smoke sim gates the batch; on pass, all sbatch jobs submit with `tag→job_id` captured. |
| `status` | `status` | `squeue` (+ `sacct` for finished), updates each ledger, prints per-state counts; batch → `[complete]` when all jobs terminal. |
| `collect` | `rollup` | Runs the batch's own `<run-dir>/scripts/rollup.py` (ledgers from before snapshots: `<remote_infra_path>/scripts/rollup.py`) on the cluster over the `.out/.err` on NFS, fetches `stats.csv` to `<repo>/.cluster-run/runs/<id>/stats.csv`, flags failed/filtered runs, diffs vs the previous rolled-up batch. Refuses incomplete unless `--force`. |
| `combine` | `combine` | Merges several complete batches into one `rollup.py -d <dirA> <dirB> …` pass (incremental experiments), using the newest batch's `rollup.py`: its snapshot, else the live infra copy. Exps go in as per-batch copies with each snapshot path mapped back to the sim tree, so a definition repeated across batches merges and only real conflicts abort. `trace_failed` applies across batches; writes no ledger, no compare. `-o` may not be an existing batch id. |

## ChampSim-specific realizations of the contract's cross-cutting rules

- **Self-contained batches** — the run dir holds everything its jobs read:
  `bin/<exe>.<ts>` (`create_jobfile.py` copies the fresh binary), one dir per
  `snapshot_dirs` entry (`cp -a` of `<sim>/<dir>` after the build; default
  `[config]`, `[configs]` for a ChampSim tree whose runtime TOMLs live there),
  `scripts/` (`cp -a` of `<infra>/scripts`; `create_jobfile.py` and `rollup.py`
  run from here) and `inputs/` (each exp's canonical `<sim>/<dir>` paths repointed
  at `<run-dir>/<dir>`; tlists untouched). An exp still reading the sim tree any
  other way (another dir, a longer alias path, `//`, `/./`, `..`, `$HOME`, `~`, a
  path split across definitions) is refused before any change on the cluster; a
  copied symlink leaving the run dir, or an exp repointed into a `snapshot_dirs`
  entry the cluster lacks, fails the submit before `create_jobfile`. So a later
  submit's sync or rebuild can't change a prior batch: ChampSim batches are
  independent. Ledger fields: `self_contained`, `snapshot` (`scripts` + each copied dir).
- **Serialized submits** — submits of one checkout hold `.cluster-run/submit.lock`
  from before the batch id is picked until `create_jobfile` returns (a waiting one
  logs so); separate checkouts of the same sim share its remote tree and must not
  submit concurrently. The batch id is claimed first: `runs/<batch>.json` is created
  exclusively as a `submitting` placeholder, and the run dir with a plain `mkdir`.
  If `create_jobfile` returns no report, `submit` says the batch MAY have queued jobs
  and keeps the placeholder (`status` lists it; `rollup`/`combine` refuse it).
- **Smoke-gate** — `--smoke-warmup/--smoke-sim` tune the gate; `--smoke-idx`
  picks which (trace×exp) pair smoke-tests.
- **Error ids** — `submit` prints stable ids: `CJ_SMOKE_FAILED` (with output
  tail), `CJ_EXE_NOT_FOUND`, `CJ_DUPLICATE_NAME`, build failure.
- **Staleness trap** — `rollup` runs the batch's snapshot `rollup.py`, so an edit
  to it reaches no snapshotted batch, even after a `submit`; to re-roll a finished
  batch with the fix,
  `rsync champsim-infra/scripts/rollup.py <cluster>:<run-dir>/scripts/`. Ledgers
  from before snapshots still run `<remote_infra_path>/scripts/rollup.py`, which
  only `submit` refreshes: edit it and roll up without an intervening `submit`,
  and the cluster runs the stale copy (symptom:
  `rollup.py: error: unrecognized arguments`). Fix:
  `rsync champsim-infra/scripts/<file>.py <cluster>:<remote_infra_path>/scripts/`.

## kratos2 facts (a configured cluster)
- Partition is **`cpu_part`** (not `compute`).
- Remote python is **`python3.10`** (needs ≥3.9 for `argparse.BooleanOptionalAction` + pyyaml), not `python3.12`.
- Poll with `squeue`; failures surface from the run outputs.
