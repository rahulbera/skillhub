# Backend contract

A **backend** is any command that performs the workload-specific execution for
`slurm-launch`. The skill is backend-agnostic: it sequences the verbs below and
applies judgment; the backend does the actual sync/build/submit/query/collect.
`config.yml`'s `backend.command` names the entrypoint (e.g.
`python3.12 /path/champsim-infra/scripts/cluster_run.py`). ChampSim's
`cluster_run.py` is the reference implementation — see
`../examples/champsim/backend-notes.md`.

A backend MUST expose these verbs. Each is invoked with `--repo <repo-root>` (so
it finds `.slurm-launch/config.yml` and the per-batch ledger) plus verb-specific
arguments.

## `configure`  (a.k.a. bootstrap)
Establish/validate the per-repo cluster config. Inputs: remote repo path, build
command, cluster; optional overrides (partition, remote interpreter, node, remote
runs base, …). Effects: write `<repo>/.slurm-launch/config.yml`, gitignore
`.slurm-launch/`, and connectivity-check the cluster. Idempotent with a `--force`
re-bootstrap.

## `submit`
Run one batch end-to-end and record it. Inputs: the job specification (however
the backend expresses a parameter sweep — trace/exp/metric files for ChampSim, a
param list for a generic sweep), optional `--label`, `--cluster`. Steps the
backend performs, in order:
1. **Sync** the repo (and any shared infra) to the cluster.
2. **Build** remotely (`config.build_command`).
3. **Snapshot** the freshly-built artifact into the batch's run dir and point
   this batch's jobs at the snapshot — so a later submit's rebuild cannot change
   a prior batch's queued/running jobs (this is what makes batches independent).
4. **Smoke-gate**: run one quick job; submit the full array ONLY if it passes.
5. **Submit** the sbatch array; capture each `tag → job_id` exactly into the
   batch **ledger**.

Output: on success, a batch id + job count. On failure, a stable `error_id`
(e.g. build failure, smoke failure, duplicate name) + reason, with **no** jobs
queued and **no** ledger written.

## `status`
Report progress of one or more batches. Reads the ledger, queries `squeue`
(active) and `sacct` (finished), updates the ledger, and prints per-state counts.
A batch becomes `complete` when every job is terminal. Read-only on the cluster.

## `collect`  (a.k.a. rollup)
Gather a complete batch's outputs into a local summary. Reads the job outputs on
the cluster, produces a summary table, fetches just that summary back to
`<repo>/.slurm-launch/runs/<batch>/`. Should refuse an incomplete batch unless
forced. Flags failed/filtered runs.

## `combine`  (optional)
Merge several complete batches into one derived table (e.g. incremental
experiments that share a trace/input set). Writes no ledger and perturbs no
batch-to-batch diff — it is a derived view.

## Ledger
The backend owns a per-batch ledger under `<repo>/.slurm-launch/` mapping each
job's `tag → job_id` plus batch state. It is the source of truth for `status`
and `collect`; the skill never reconstructs job ids by hand.
