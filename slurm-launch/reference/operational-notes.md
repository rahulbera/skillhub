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

## Self-contained batches → independent, parallel batches
A queued job reads its inputs when it **starts**, which can be after a later
`submit`'s `rsync --delete` + rebuild has replaced the live tree. Snapshotting only
the binary is not enough: config files and wrapper scripts are read at start too,
so a sync would still silently reconfigure every unstarted job. A self-contained
batch keeps everything its jobs read at run time in its own run dir:
- **binary** — a copy of the fresh build (a hardlink shares its inode, so a build step
  that writes the binary in place, such as `cp` or `strip`, would change it);
- **config** — a full copy of each repo dir holding files jobs read at start, from a
  per-repo list, since trees differ (ChampSim keeps runtime TOMLs in `configs/`,
  Hermes its `.ini`s in `config/`);
- **scripts** — a full copy of the infra scripts; job generation, the per-job
  wrapper and `collect` all run from this copy;
- **inputs** — the staged job spec, rewritten so config paths name the copy (data
  paths such as trace lists are not rewritten).

The backend must **refuse**, before queueing anything, a job spec that would still
read the live tree: rewrite only the canonical spelling and refuse the non-canonical
spellings it can resolve (a longer alias path, `//`, `/./`, `..`, `$HOME`, `~`). The
check is textual, so shell forms it cannot resolve (quoted pieces, `$USER`, `$PWD`)
get through; document those as known limitations. A copied
symlink that resolves outside the run dir still reads the live tree, so refuse that
too. Consequence: batches are independent — submitting while other batches of the
same repo are queued or running is safe and must not be blocked; no need to drain or
wait. Confirm your backend is self-contained before relying on this.

Submits themselves are not independent: each syncs, builds and snapshots the one live
remote tree, so an overlapping submit could snapshot another's config or binary. The
backend must serialize the submits of one checkout (ChampSim's holds a per-checkout
lock until its jobs are launched), and separate local checkouts of one repo must not
submit concurrently.

## The ledger is the source of truth
The backend captures `tag → job_id` at submit into a per-batch ledger. `status`
and `collect` read it; never reconstruct job ids by hand. The backend claims each
batch id exclusively before any remote work, so two submits never share a ledger. A
submit that fails before it launches queued nothing — re-submit. One that stops while
launching, or loses the launcher's report, MAY have queued jobs and must say so: check
`squeue` before re-submitting.

## "Only submit syncs remote infra" — the staleness trap
Typically only `submit` rsyncs code/infra to the cluster; `status`/`collect` just
invoke scripts already there. A self-contained batch collects with its **own
snapshot**, so a later edit to a remote-executed script never reaches it — not
through a sync, not through a `submit`; to re-collect a finished batch with the
fix, copy the script into that batch's snapshot. A batch without a snapshot (or a
backend that runs the live copy) has the opposite trap: edit a **remote-executed**
script, run `collect` *without* an intervening `submit`, and the cluster runs the
**stale** copy (classic symptom: a remote script erroring on a flag it doesn't
recognize yet). Fix: rsync the changed script before re-running, or run a `submit`
(which refreshes everything).

## SSH, sandbox, and network
SSH/rsync need real network. If Bash runs sandboxed, the first `ssh`/`rsync`
fails — run the authorized cluster ops with the sandbox disabled, or have the
user prime `ssh <cluster> true` so auth is cached. The `configure` connectivity
check is the canary. The first SSH of a session is worth a heads-up; never assume
standing approval for remote actions across turns.

## Long submits → background
Build + smoke can take several minutes. Run `submit` in the background (or with a
generous timeout) so a foreground timeout doesn't kill it mid-build and leave no
record of its jobs.

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
