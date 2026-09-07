# Operational notes (AWS)

Cross-cutting wisdom for launching batch jobs on an elastic AWS ParallelCluster
Spot Slurm cluster. Read once before your first submit.

## We own the head node → everything over SSM (not SSH)
The head node is ours, so we drive it with **AWS Systems Manager**
(`aws ssm send-command` for run-to-completion, `start-session` for an interactive
shell) — **no SSH keys**. Jobs on the head node run as the `remote_user`
(`ubuntu`). Never hand-roll job submission outside the backend; job-id capture
must stay in the backend so the ledger is exact.

## One cluster, many projects and people

The head node is shared. Every project gets its own namespace, set by `project` and
`remote_project_root` in the config, and it applies on BOTH sides:

    s3://<results>/bootstrap/<project>/   this project's job wrapper + batch files
    s3://<results>/results/<project>/     this project's output (what `collect` syncs)
    <remote_project_root>/
      Hermes/      that project's ChampSim checkout + built binary
      results/     Slurm .out/.err logs only
      run-assets/  wrapper, *.exps, *.tlist -- submission happens HERE, never in $HOME

This is not cosmetic. Before namespacing there was a single
`bootstrap/champsim-job.sh` and submission ran in `$HOME`, so a second project or a
second person submitting would overwrite the first one's wrapper and scratch files
mid-flight. `load_cfg` now refuses to run if a config lacks these keys rather than
silently falling back to the shared layout.

Two details worth knowing:
- The bundled `scripts/champsim-job.sh` is a **template**. Its `#SBATCH -o/-e` lines
  cannot use a shell variable (Slurm parses them before any shell runs), so the backend
  substitutes `{{PROJECT_ROOT}}` at upload time, per project.
- Results are NOT kept on the head node. A job writes stats to the compute node's
  `/scratch`, uploads to S3, and deletes the local copy; only the small Slurm logs stay
  on NFS. Keeping a second copy on the head node grew /home by ~14 GB per campaign on a
  97 GB volume.

## The head-node lifecycle (the big difference from an on-prem cluster)
- The head node **auto-stops** after `idle_stop_minutes` (default **30**) of an
  empty queue **and** no other activity (a systemd timer, `scripts/deploy-idle-stop.sh`).
  A stopped head costs only its EBS root (~$8/mo) instead of ~$0.15/hr.
- The guard treats **all** of these as busy: a non-empty Slurm queue, a logged-in
  user or SSM Session Manager shell, **a running `aws ssm send-command`**
  (`ssm-document-worker`), a workload process on the head node, and the explicit
  lock `/var/lib/champsim-busy`. The send-command guard matters: a run-command is
  *not* an interactive session, so without it a long automated setup step gets shut
  down underneath itself. For multi-step work hold the lock
  (`sudo touch /var/lib/champsim-busy`, refreshed; it goes stale after 45 min so a
  crashed job can't pin the node forever). Keep the workload pattern tight — a loose
  one self-matches the guard script's own path and pins the node up permanently.
- Don't set `idle_stop_minutes` below ~30: the window has to comfortably exceed your
  longest gap between commands during interactive setup.
- Every `submit`/`status`/`collect` **wakes it first** via `scripts/aws-wake.sh`
  (idempotent: start-if-stopped → wait running → SSM online → `slurmctld` ready,
  ~30 s). A wake is **dollar-free** — stop/start are free, EBS is flat, and the
  ~1 min of boot is a fraction of a cent.
- **Compute** Spot nodes scale `0 → N → 0` on their own (Slurm power-save +
  short `ScaledownIdletime`). You never manage them; `submit` just queues jobs and
  the fleet grows to fit, then drains.

## Trace-local staging (no shared FS)
Inputs are **not** on a shared filesystem. Each job's wrapper stages the one input
it needs from S3 to node-local NVMe `/scratch` (a prolog), runs against the local
copy, and pushes its result to `s3://<results>/results/` (an epilog). Same-region
S3↔EC2 transfer is free and fast. `/scratch` is auto-provisioned by the AMI as a
multi-TB ext4 volume on the instance store.

## Spot economics & requeue
- Compute is **Spot only** (on-demand is denied for compute by IAM). Diversify
  across instance types + AZs (the cluster config already does) so the fleet fills
  from many capacity pools.
- A Spot reclaim kills a running job. ChampSim runs are cheap to re-run, so prefer
  `--requeue`/re-submit over mourning a lost job. Size batches to the account's
  Spot vCPU quota (the real ceiling), not the queue `MaxCount`.
- Instance granularity: N jobs pack onto `ceil(N / cores_per_node)` whole nodes;
  a few idle cores on the last node cost pennies — not worth optimizing.

## Pre-flight on a new cluster (cheap; do once)
Read-only, over SSM, after a wake:
- **Partition exists:** `sinfo -h -o '%P'` — set `partition` to a real Spot one.
- **Binary builds / is present:** `remote_repo_path` has the repo; `build_command`
  produces `binary`.
- **Inputs resolve:** `aws s3 ls s3://<traces>/<trace_prefix>/` returns objects.
- **EC2 Spot service-linked role exists** (see "Why a command failed").

## Smoke-gate before the full array
Always run ONE quick job first; submit the array only if it passes. A bad build,
a wrong knob, or a missing `--trace_version=2` fails the smoke job cheaply instead
of failing (and paying Spot boot for) a whole array. `submit` owns this gate.

## The ledger is the source of truth
`submit` captures `tag → job_id` into `<repo>/.aws-launch/runs/<batch>/ledger.json`.
`status` and `collect` read it; never reconstruct job ids by hand. If `submit`
dies before writing the ledger, no jobs were queued — re-submit.

## Read-only vs mutating
`status`/`list` are read-only. `submit` (job submission, S3 writes) and `collect`
(S3 reads + local writes) change state. `submit` also causes **Spot instances to
launch** — a first large scale-out in a session is worth flagging to the user.

## Polling
You can't poll unattended. Offer an opt-in loop (`/loop 10m` running `status`)
while the user is present. When a batch reaches `complete`, offer to collect.

## Why a command failed (quick map)
| Symptom | Cause |
|---|---|
| Jobs stuck `PENDING (Nodes ... DOWN)`, `slurm_resume.log` shows `ServiceLinkedRoleCreationNotPermitted` | The account is missing the **EC2 Spot service-linked role**. An admin runs once: `aws iam create-service-linked-role --aws-service-name spot.amazonaws.com`. |
| `AccessDenied` on any non-`region` call | Region lock — every call must use the config `region`. |
| `AccessDenied` on `RunInstances` for compute | Compute must be Spot (on-demand denied); or the instance type is off-allowlist. |
| `AccessDenied` on `budgets:*` | Intentional; the deployer can't read/alter budgets. Surface to admin. |
| Sim crashes `va_to_pa Assertion 0` on a `.champsim2.zst` trace | v2 trace read as v1 — add `--trace_version=2` to the knobs. |
| STS/credential error mid-run | 1 h token expired; it auto-refreshes from the source profile — just retry. |

## Sandbox / network
SSM and S3 calls need real network. If the local shell is sandboxed, the first
`aws` call fails — run the authorized cluster ops with the sandbox disabled, or
prime `AWS_PROFILE=<p> aws sts get-caller-identity` first. Never assume standing
approval for remote/mutating actions across turns.
