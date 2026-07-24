# Operational notes

Cross-cutting wisdom for launching batch jobs on an SSH-only Slurm cluster.
Backend-independent — these apply whether the backend is ChampSim's
`cluster_run.py` or a raw-sbatch sweep. Read once before your first submit.

## Agent-barred login node → everything over SSH
Many clusters bar AI agents on the login node. Never run an agent on the cluster.
All cluster work is `ssh`/`rsync`/`sbatch` shelled out from the local machine
through the backend. Never hand-roll `ssh` around the backend for submission —
job-id capture must stay inside the backend so the ledger is exact.

## Pre-flight on a new cluster (cheap; do once before the first submit)
Mismatches here are the usual cause of a failed first submit. Verify over SSH,
read-only:
- **Partition exists.** `ssh <cluster> "sinfo -h -o '%P'"` — the default is marked
  `*`. Set `slurm.partition` to a real one.
- **Usable remote interpreter + deps.** Check the version and any imports the
  backend needs (e.g. `ssh <cluster> "python3 --version; python3 -c 'import yaml'"`).
- **Tools present.** `sbatch squeue sacct`, plus any the workload needs
  (decompressors like `zstd`/`xz`, compilers).
- **Writable remote base**, and **one real input path resolves**
  (`ssh <cluster> "ls <an input path>"`).

Fix via the backend's `configure --force …` or by editing `config.yml`.

## Smoke-gate before the full array
Always run ONE quick job first and submit the whole array only if it passes. A
bad build or a path typo fails the smoke job cheaply instead of failing hundreds
of queued jobs. The backend's `submit` owns this gate.

## Snapshot build artifacts → independent, parallel batches
If the backend snapshots the freshly-built artifact (e.g. hardlinks the binary)
into each batch's run dir and points that batch's queued jobs at the snapshot,
then a later `submit`'s `rsync --delete` + rebuild **cannot** alter a prior
batch's queued/running jobs. Consequence: batches are independent — you do NOT
need to drain or wait for earlier batches before submitting a new one. Confirm
your backend snapshots before relying on this.

## The ledger is the source of truth
The backend captures `tag → job_id` at submit into a per-batch ledger. `status`
and `collect` read it; never reconstruct job ids by hand. If a submit dies before
writing the ledger, no jobs were queued — re-submit.

## "Only submit syncs remote infra" — the staleness trap
Typically only `submit` rsyncs code/infra to the cluster; `status`/`collect`
assume the remote copy is current and just invoke remote scripts. So if you edit
a **remote-executed** script and then run `collect` *without* an intervening
`submit`, the cluster runs the **stale** copy (classic symptom: a remote script
erroring on a flag it doesn't recognize yet). Fix: rsync the changed script
before re-running, or run a `submit` (which refreshes everything).

## SSH, sandbox, and network
SSH/rsync need real network. If Bash runs sandboxed, the first `ssh`/`rsync`
fails — run the authorized cluster ops with the sandbox disabled, or have the
user prime `ssh <cluster> true` so auth is cached. The `configure` connectivity
check is the canary. The first SSH of a session is worth a heads-up; never assume
standing approval for remote actions across turns.

## Long submits → background
Build + smoke can take several minutes. Run `submit` in the background (or with a
generous timeout) so a foreground timeout doesn't kill it mid-build and leave no
ledger.

## Read-only vs mutating
`status`/`list` are read-only. `submit`/`collect` mutate the cluster (rsync
`--delete` on the repo dir, job submission, result writes). Be explicit about
which you're about to run.

## Polling
You can't poll unattended. If the user wants to wait for a batch, offer an opt-in
loop (e.g. `/loop 10m` running `status`) while they're present. When a batch
reaches `complete`, offer to collect.

## Config-file hygiene
Job-spec files parsed remotely must be well-formed for the remote parser — e.g.
PyYAML rejects tab indentation (`found character '\t' that cannot start any
token`). Validate/convert before submitting so a parse error doesn't surface only
on the cluster.
