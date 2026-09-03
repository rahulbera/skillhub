# Backend contract (AWS)

A **backend** performs the workload-specific execution for `aws-launch`. The
skill sequences the verbs below and applies judgment; the backend does the actual
wake/build/submit/query/collect over SSM + S3. `config.yml`'s `backend.command`
names the entrypoint. The bundled `scripts/aws_launch.py` is the reference
implementation (ChampSim/Hermes); swap it for another workload by pointing
`backend.command` elsewhere and keeping the same verbs.

Each verb is invoked as `<command> <verb> --repo <repo-root> [args]` and reads
`<repo>/.aws-launch/config.yml`.

## `configure` (bootstrap)
Establish/validate the per-repo config. Effects: validate the AWS profile
(`aws sts get-caller-identity`), discover the head node by cluster tag, check the
S3 buckets + Slurm partition, write `config.yml`, and gitignore `.aws-launch/`.
Idempotent with `--force`. If the profile is missing, print the exact
`~/.aws/config` stub (see credentials.md) and stop.

## `submit`
Run one batch end-to-end and record it. Inputs: a trace list + an experiment/knob
list (however the workload expresses a sweep), optional `--label`. Steps:
1. **Wake** the head node (`scripts/aws-wake.sh`).
2. **Ensure built**: build `binary` from `remote_repo_path` via `build_command`
   if absent (over SSM).
3. **Smoke-gate**: run ONE quick job (short instruction counts); proceed only if
   it produces a valid result.
4. **Submit** the sbatch array to `partition` — each job runs the `job_wrapper`
   (stage input S3→/scratch, run `binary` + knobs, push result→S3).
5. **Ledger**: capture each `tag → job_id` into
   `runs/<batch>/ledger.json` with batch state `submitted`.

Output: batch id + job count on success; a stable `error_id` (build/smoke/dup) +
reason with **no** jobs queued on failure. Long-running → callers run it in the
background.

## `status`
Report progress. Wakes the head node, queries `squeue` (active) + `sacct`
(finished) over SSM, updates the ledger, prints per-state counts and the current
Spot compute-node count. Batch → `complete` when every job is terminal.
Read-only on cluster *state* (but it does wake the head node).

## `collect` (rollup)
Gather a complete batch's results. Reads each job's result object from
`s3://<results>/results/`, builds a local summary table (e.g. IPC per trace/exp),
writes it to `runs/<batch>/summary.csv` (+ a human-readable table). Refuses an
incomplete batch unless `--force`. Flags failed/missing jobs.

## Ledger
The backend owns `runs/<batch>/ledger.json`: `{batch, label, state, jobs:[{tag,
job_id, trace, exp}], submitted_at}`. It is the source of truth for `status` and
`collect`.

## Lifecycle primitives (shared)
- `scripts/aws-wake.sh` — idempotent head-node wake (start → SSM online →
  slurmctld ready). Every mutating/querying verb calls it first.
- `scripts/deploy-idle-stop.sh` — installs the head-node auto-stop timer
  (`idle_stop_minutes` + active-session guard). Run once at cluster setup.
- `scripts/champsim-job.sh` — the reference per-job wrapper (S3 stage-in →
  run → S3 stage-out); the backend uploads it to `s3://<results>/bootstrap/`.
