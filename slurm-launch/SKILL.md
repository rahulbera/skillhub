---
name: slurm-launch
description: Launch, monitor, and collect batch jobs on a remote Slurm cluster from your local machine over SSH, when the cluster login node bars AI agents. Drives a per-repo backend (any command exposing configure/submit/status/collect) through a fixed playbook — bootstrap, pre-flight, submit an sbatch array, poll, roll up results. Use for "launch/submit my jobs on the cluster", "check my cluster jobs", "roll up the results". ChampSim's cluster_run.py is the reference backend.
---

# Slurm launch

One-shot orchestration of batch jobs on an SSH-only Slurm cluster. Many clusters
bar AI agents on the login node, so **everything on the cluster happens over
SSH** — never run an agent on the cluster. The skill supplies the *sequencing
and judgment*; a per-repo **backend** does the workload-specific execution.

This is the generic playbook. The execution layer is pluggable: any command that
exposes the verbs in `reference/backend-contract.md` is a valid backend.
ChampSim's `cluster_run.py` is the reference implementation — see
`examples/champsim/`.

## Config

Each repo carries a gitignored `<repo>/.slurm-launch/config.yml` (schema:
`reference/config-template.yml`) naming the cluster, remote paths, build command,
and the **backend command**. Read `reference/operational-notes.md` once — it holds
the cross-cutting wisdom (pre-flight, smoke-gate, snapshotting, ledger, SSH/sandbox,
polling) that applies to every backend.

## Playbook

Create a todo per step; work them in order. Each step calls the configured
backend's verb — you decide *whether* and *when*, the backend does the *how*.

### 1. Orient
- Resolve the repo: `git -C <cwd> rev-parse --show-toplevel` (fall back to cwd).
- Look for `<repo>/.slurm-launch/config.yml`.
  - Absent → **Bootstrap**.
  - Present → list logged batches via the backend. If any is `submitted`/`running`,
    **tell the user** and **offer** a status check (an SSH call — get
    acknowledgement first; never auto-run remote ops). If any is `complete`
    (done, not collected), offer **Collect**.

### 2. Bootstrap (first use in a repo)
Ask the user (conversationally) for what can't be derived — the **remote repo
path** and the **build command** — confirm the **cluster**, then call the
backend's `configure` verb. It writes `config.yml` and gitignores
`.slurm-launch/`. The user can edit `config.yml` later.

### 3. Pre-flight (once, before the first submit on a new cluster/repo)
Mismatches here are the usual cause of a failed first submit. Verify over SSH
(read-only) per `reference/operational-notes.md`: partition exists, a usable
remote interpreter + deps, required tools (`sbatch squeue sacct`, any
decompressors), a writable remote base, and one real input path resolves.

### 4. Submit a batch
Call the backend's `submit` verb with the user's job inputs. The backend must:
sync code to the cluster, build remotely, **smoke-gate** (run one quick job;
only proceed if it passes), submit the sbatch array, and capture `tag→job_id`
into a ledger.
- Success → report batch id + job count.
- Failure → relay the backend's stable `error_id` + reason and stop (no jobs
  queued).
- **Batches are independent** if the backend snapshots build artifacts per batch
  (see operational-notes) — launch in parallel freely; no need to drain earlier
  batches first.
- Submits can take minutes (build + smoke) — run in the background so a
  foreground timeout doesn't kill it mid-build.

### 5. Check status
Call the backend's `status` verb. It queries `squeue`/`sacct`, updates the
ledger, and reports per-state counts; a batch flips to `complete` when every job
is terminal. **Poll only while the user is present** — you can't poll unattended;
offer an opt-in loop (e.g. `/loop 10m`) if they want to wait.

### 6. Collect
When a batch is `complete`, call the backend's `collect` verb to gather outputs
into a local summary. Some backends also offer `combine` to merge several
batches into one table.

## Rules
- Everything on the cluster is over SSH; never run an agent there, never
  hand-roll `ssh` around the backend for submission (job-id capture lives in the
  backend).
- `submit`/`collect` **mutate** the cluster (rsync `--delete`, job submission);
  `status`/`list` are read-only. Be explicit about which you're about to run.
- SSH/rsync need real network — if Bash is sandboxed the first call fails; run the
  authorized cluster ops with the sandbox disabled, or prime `ssh <cluster> true`
  first. The first SSH of a session is worth a heads-up; never assume standing
  approval for remote actions across turns.
