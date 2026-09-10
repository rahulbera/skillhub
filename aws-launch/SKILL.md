---
name: aws-launch
description: Launch, monitor, and collect batch jobs on an elastic AWS ParallelCluster Spot Slurm cluster from your local machine. Drives the cluster over AWS SSM (no SSH keys) through a pluggable per-repo backend (configure/sync/submit/status/collect), waking the auto-stopping head node on demand and staging inputs/results through S3. Use for "launch/submit my jobs on AWS", "check my AWS cluster jobs", "roll up the AWS results". ChampSim/Hermes is the reference backend.
---

# AWS launch

One-shot orchestration of batch jobs on an **elastic, Spot-backed AWS
ParallelCluster Slurm cluster**. Unlike an on-prem login node, this cluster is
*yours*: you drive its head node over **AWS Systems Manager (SSM)** — no SSH keys
— and it **auto-stops when idle** to save money, so the skill **wakes it on
demand**. Inputs and results move through **S3**; compute is **Spot** (elastic,
scale-to-zero, requeue-on-reclaim).

The skill supplies the *sequencing and judgment*; a per-repo **backend** does the
workload-specific execution (build, submit, collect). The bundled
`scripts/aws_launch.py` is the reference AWS backend and ChampSim/Hermes is the
reference workload — see `examples/champsim/`.

## Credentials — the only real setup step
All AWS access is through a **named AWS profile** — **never raw keys in the repo
or config**. One-time, the user configures the profile their admin gave them
(`aws configure --profile aws-launch`, or an assume-role / SSO profile);
`config.yml`'s `aws_profile` names it. The skill validates it
(`aws sts get-caller-identity`) at bootstrap. Full walkthrough (built for interns
and collaborators): **`reference/credentials.md`** — read it before the first use
on a new machine.

## Config
Each repo carries a gitignored `<repo>/.aws-launch/config.yml` (schema:
`reference/config-template.yml`) naming the AWS profile, region, cluster, S3
buckets, Slurm partition, and the build/job commands. Read
`reference/operational-notes.md` once — the AWS-specific wisdom (wake lifecycle,
Spot/requeue, S3 staging, cost, SSM, region lock).

## Shared cluster — the layout convention (enforced, not optional)

One head node and one pair of buckets serve several projects and several people. Follow
this layout; the backend **refuses to run** if the config violates it.

    project: <owner>/<project>              e.g. rbera/hermes-uncore   (owner-first, one '/')
    remote_project_root: /home/<user>/<owner>/<project>
    remote_repo_path:    <remote_project_root>/Hermes

    s3://<traces>/...                              SHARED, READ-ONLY. Never write here.
    s3://<results>/<owner>/<project>/bootstrap/    job wrapper + batch files
    s3://<results>/<owner>/<project>/results/      output (what `collect` syncs)
    <remote_project_root>/{Hermes,champsim-infra,results,run-assets}

Owner-first is the whole point: everything one person owns sits under a single top-level
prefix, so ONE IAM statement (`<results-bucket>/<owner>/*`) scopes them completely.

`load_cfg` rejects, with an explanation, a config that: is missing `project` or
`remote_project_root`; still contains the template's `<owner>/<project>` placeholders;
uses a bare `project` name with no owner segment; has a `remote_project_root` that does
not end with `<owner>/<project>` (results would land in one owner's S3 prefix while files
are written to another's directory); or points `remote_repo_path` outside the project
root (two projects would share one ChampSim build).

**Never give two projects the same full `<owner>/<project>` value** (the project name
alone may repeat across different owners), and never point a job at the traces bucket
for writing. Full rationale in `reference/operational-notes.md`.

## Playbook
Create a todo per step; work them in order. Each step calls the configured
backend's verb; the backend drives the cluster over SSM.

### 1. Orient
- Resolve the repo: `git -C <cwd> rev-parse --show-toplevel` (fall back to cwd).
- Look for `<repo>/.aws-launch/config.yml`.
  - Absent → **Bootstrap**.
  - Present → list logged batches via the backend. If any is `submitted`/`running`,
    **tell the user** and **offer** a status check. If any is `complete`
    (not collected), offer **Collect**. A run dir with `attempt.json` and no ledger is
    an attempt that may have queued jobs: offer a status check (step 5).

### 2. Bootstrap (first use in a repo)
Confirm the **AWS profile** and **cluster name** with the user, then ask for the
**project namespace** — there is no sensible default and `configure` requires it:

    --project <owner>/<project>        e.g. rbera/hermes-uncore

**Ask the human for this value, and confirm it back to them a second time before
running `configure`.** Say explicitly what you are about to use, e.g.:

> "I'll namespace this project as `mihai/pythia-sweep`. That claims
> `s3://champsim-results-all/mihai/...` and `/home/ubuntu/mihai/pythia-sweep` on the
> head node. Please confirm that `mihai` is your own owner name, and that you aren't
> already using the project name `pythia-sweep` yourself."

Only the **pair** has to be unique. Two people may each have a `pythia-sweep`;
`mihai/pythia-sweep` and `todor/pythia-sweep` are different prefixes and never collide.
What must not happen is claiming an owner name that isn't yours, or reusing one of your
own project names.

Get an explicit yes. Do **not** guess it from the directory name, the git remote, or
the user's shell username — a wrong or duplicated value is not a private mistake:

- Reusing someone else's `<owner>` writes your results into **their** prefix. The
  cluster's node role has bucket-wide write access, so **AWS will allow it** and
  nothing will fail — their rollups just silently gain your data, or lose to it.
- Reusing an existing `<owner>/<project>` overwrites that project's job wrapper and
  batch files in S3 mid-flight, corrupting a run that is already going.

Neither is recoverable from the skill's side, and neither announces itself. Thirty
seconds of confirmation avoids a bad day for someone who is not in the room.

Then call the backend's `configure` verb. It validates the profile
(`sts get-caller-identity`), discovers the head node by tag, checks the S3 buckets and
the Slurm partition, enforces the layout convention, and writes `config.yml`
(gitignoring `.aws-launch/`). Idempotent with `--force`. If the profile is missing,
point the user at `reference/credentials.md`.

### 3. Pre-flight (once per new cluster/repo)
**Wake the head node first** — it may be stopped. Verify, read-only: partition
exists (`sinfo`), the S3 trace prefix has objects, and the **EC2 Spot
service-linked role exists** (its absence blocks every Spot launch — see
operational-notes). For the jobfile route also check the head node's `python3` has
PyYAML (`python3 -c 'import yaml'`) and that `sync:` mirrors `champsim-infra`. Then run
`sync --build` (this one mutates: S3 upload, remote files) to confirm the source mirrors
and builds. Mismatches here are the usual cause of a failed first submit.

### 4. Submit a batch
Call the backend's `submit` verb with the job spec (a trace list × an experiment
list) in exactly one of two routes; the backend refuses a mix:
- **jobfile route** — `--tlist T.. --exp E.. --mfile M.. [--smoke-idx N]`: champsim-infra's
  YAML inputs, launched by its `create_jobfile.py`. Each job fetches its trace into
  `trace_cache_dir` and writes `<tag>.out/.err` into the batch's run dir. Prefer it
  whenever the user has tlist/exp/mfile files.
- **wrapper route** — `--traces file --exps "name=knobs;..." [--spot-smoke]`: trace keys and
  inline knob strings, run by the bundled `champsim-job.sh` (each job stages its input
  S3→node-local NVMe `/scratch`, runs, pushes its result→S3). Experiment names use letters,
  digits and `._+-`, and trace keys hold no whitespace (`bad_input_name`, `bad_input`).

The backend, in order: validates the label and inputs **locally**, **wakes** the head node,
**syncs** the local source onto it (sha256-verified), **rebuilds** and freezes a
**self-contained batch** (below) under the same lock, **smoke-gates** on the head node (runs
one quick job from that snapshot; proceeds only if it passes), submits the sbatch jobs to the
Spot queue, and records `tag → job_id` in the batch **ledger**. The head-node launch stops
itself 300 s before `launch_timeout_s`.
- Success → report batch id + job count.
- Failure → relay the backend's `error_id` + reason; **no** jobs queued, except:
  - queued and recorded: `submit_partial` (some sbatch calls failed; the launched jobs are
    in the ledger — tell the user which pairs are missing) and `ledger_incomplete` (fewer
    ids than pairs; the ledger keeps the ones it got).
  - MAY be queued, not recorded: `jobfile_timeout`, `jobfile_no_report`, `jobfile_already_launched`
    and `submit_no_record` (the batch's jobs), and `smoke_timeout` and, when its message says so,
    `smoke_submit_failed` (the `--spot-smoke` gate job, named for `scancel` when its id is known)
    — and so may a submit that was killed or interrupted after freezing its batch. Tell the user
    to check `squeue` and the head-node record the error names before anything is resubmitted;
    `status --batch <batch>` recovers the ledger once the attempt's S3 record is there.
- Submits can take minutes (build + smoke) — **run in the background**.
- `--label` names the batch (letters, digits, `._-`) and can be used once, even by an attempt
  that failed after freezing its batch (`batch_exists`: check `squeue`, then pick a new one). A
  running submit claims its label (`runs/<batch>.claim`), so a second submit of it, on either
  route, refuses with `batch_exists` before waking.
- `--no-sync` skips the sync and rebuild. Use it only to add a batch on a build you already
  synced; nothing then checks the head node matches your tree.
- A submit that scales **many** Spot nodes for the first time in a session →
  flag the scale-out and get the user's ok first.

### 5. Check status
Call the backend's `status` verb — wakes the head node if needed, queries
`squeue`/`sacct` over SSM, updates the ledger, and reports per-state counts plus
the current **Spot node count** (the elasticity readout). A batch becomes
`complete` when every job is terminal. **Poll only while the user is present**;
offer an opt-in loop (e.g. `/loop 10m`) if they want to wait.

A run dir with `attempt.json` but no ledger is a submit that froze its batch and then was killed,
interrupted, or never saw its job-id record. `status` first tries a read-only recovery from that
attempt's own S3 record (`report.json`, or the wrapper route's per-attempt `submitted.txt`); a record
that is there becomes the ledger, and status carries on. Each one it cannot recover is listed as
`state=unknown — may have queued jobs`, and `status --batch <batch>` on it exits `batch_unknown`:
relay that, and check `squeue` before anything is resubmitted.

### 6. Collect
When a batch is `complete`, call the backend's `collect` verb; it refuses an incomplete
batch unless `--force`. It writes into `<repo>/.aws-launch/runs/<batch>/`:
- **jobfile route** — runs the batch's own `rollup.py`, on its own inputs, **on the head
  node**, and brings back only `stats.csv` and `rollup_report.json`, never the raw
  `.out`/`.err`. It prints the rollup summary and every non-ok run, and flags rows whose
  `ipc` is 0 (a failed run, or a stat its `.out` lacks) — relay those to the user.
- **wrapper route** — syncs each job's result from S3 and builds `summary.csv`
  (IPC per trace/exp) locally. The sync is incremental, and same-region reads are free
  (egress to a laptop is $0.09/GB with 100 GB/month free).

Either way, **check one job's output size before launching hundreds**: a debug knob left
on in a shipped config once made each result 37 MB instead of 0.3 MB. See "Collecting
results" in `reference/operational-notes.md`.

## Source sync — the head node builds your tree, not whatever it has
The head node keeps whatever checkout it last had. Without a sync, a submit would build,
smoke-test and launch code of unknown age, and nothing would fail. `sync` (and every `submit`)
mirrors the work trees listed under `sync:` in `config.yml` — tracked plus untracked-not-ignored
files, as they are on disk — through S3, `rsync --delete`s them into place leaving `protect` paths
(head-node build products) alone, re-hashes every file on the head node against the local
manifest, and rebuilds: one head-node command under a per-project lock. The backend enforces:
- **Opt-in.** A config without `sync:` refuses and prints a proposed entry to review.
- **Safe under running work.** Every batch runs from its own snapshot (below), so a sync or
  rebuild never changes a pending or running job, and sync never waits for the queue.
- **Never outside the project.** A destination that is, or passes through, a symlink out of the
  project root is refused, and `results/` and `run-assets/` can never be targets.
- **Everything else under a synced directory is deleted** — a stale `.git`, and any head-node copy
  of a path listed under `exclude` (exclude means "do not ship", not "keep on the head node").
- **Only a fresh binary counts.** The old binary stays until the build replaces it; a build that
  fails or leaves an older binary is `build_failed`.
Provenance (git HEAD, uncommitted changes, digests, binary hash) lands in
`.aws-launch/sync/<stamp>/summary.json` and in the batch ledger.

## Self-contained batches — a later sync cannot reach a queued job
A queued job reads its binary, its `--config` files and its scripts when it **starts**, which
can be hours after `submit`. So each batch runs from its own copy of all of them, taken inside
the sync's lock right after the build, under `<remote_project_root>`:

    results/<batch>/        jobfile route: the run dir
      bin/<exe>.<stamp>     a copy of the fresh binary; create_jobfile runs it as given
      config/  scripts/     cp -a of each snapshot_dirs entry (default config) and <remote_infra_path>/scripts
      inputs/               tlist, exp, <exp>.resolved.yml, mfile
      jobfile.sh, report.json, <tag>.out/.err
    run-assets/<batch>/     wrapper route
      bin/<exe>.<stamp>  config/  champsim-job.sh (rendered to run that binary)

The binary is a real copy, not a hard link, so no later build reaches it, whether the build
renames a new binary into place or writes over the old one. The copy is checked before anything
runs from it: its binary must be the one this run built and each copied dir must match the sync
manifest, or the submit stops with `snapshot_mismatch`; a symlink in a copied dir that leads out
of the batch is `snapshot_failed`. `snapshot_dirs` (config key, default `[config]`) names the dirs
under `remote_repo_path` a batch copies. Experiment paths into them — `{REMOTE_REPO}/config/…` or
`<remote_repo_path>/config/…` — are pointed at the batch's copy when spelled canonically; the
jobfile route first expands each exp's `$(VAR)`s as create_jobfile does (`unresolved_variable` if
one is left) and writes `<exp>.resolved.yml` without definitions, keeping the authored file. Any
other reference to the live sim or infra tree that the backend can resolve is refused before the
head node wakes (`live_tree_reference`). Definitions are expanded first, and so are `//`, `/./`,
`..` (a relative path counts from the batch dir the jobs start in), `$HOME`, `${HOME}` and `~` (as
`/home/<remote_user>`); the message shows the canonical spelling. Other shell variables (`$USER`,
`$PWD`, …) are not resolved, so spell paths through `{REMOTE_REPO}`. Trace
paths are not rewritten. So syncing or submitting while other batches of the project are pending
or running is safe. With `--no-sync` the batch is still frozen under the lock, and its ledger says
nothing was compared. A label is single-use (`batch_exists`, `run_dir_exists`).

## The lifecycle (unique to AWS — the thing to internalize)
- The head node **auto-stops** after the idle threshold (systemd timer + an
  active-session guard so it won't stop under an open session). Every
  `submit`/`status`/`collect` **wakes it first** (`scripts/aws-wake.sh`,
  idempotent, ~30 s). Compute Spot nodes scale `0→N→0` on their own (short idle).
- **Cost at rest ≈ the head node's EBS root only (~$8/mo).** A wake is
  dollar-free and ~30 s. See operational-notes for the full cost model.

## Rules
- All cluster ops are over **SSM** (`send-command` / `start-session`) — never
  SSH, never a shared SSH key. Job-id capture lives in the backend; never
  hand-roll it.
- **Never handle raw AWS keys** — rely on the configured profile (keys live only
  in `~/.aws/`).
- `sync`/`submit`/`collect` **mutate** (S3 writes, remote files, job submission);
  `status` only reads the queue but **wakes** the head node first. Be explicit about
  which you're about to run.
- Region is **locked** (config `region`); every AWS call is scoped to it.
