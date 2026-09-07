---
name: aws-launch
description: Launch, monitor, and collect batch jobs on an elastic AWS ParallelCluster Spot Slurm cluster from your local machine. Drives the cluster over AWS SSM (no SSH keys) through a pluggable per-repo backend (configure/submit/status/collect), waking the auto-stopping head node on demand and staging inputs/results through S3. Use for "launch/submit my jobs on AWS", "check my AWS cluster jobs", "roll up the AWS results". ChampSim/Hermes is the reference backend.
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

## Shared cluster — namespace before you submit

One head node hosts several projects and several people. `project` and
`remote_project_root` in the config namespace BOTH the S3 keys
(`bootstrap/<project>/`, `results/<project>/`) and the on-node directory
(`<remote_project_root>/{Hermes,results,run-assets}`). The backend refuses to run
without them, because the old shared layout let a second project or person overwrite
the first one's job wrapper and scratch files mid-flight. Never give two projects the
same `project` value. Details in `reference/operational-notes.md`.

## Playbook
Create a todo per step; work them in order. Each step calls the configured
backend's verb; the backend drives the cluster over SSM.

### 1. Orient
- Resolve the repo: `git -C <cwd> rev-parse --show-toplevel` (fall back to cwd).
- Look for `<repo>/.aws-launch/config.yml`.
  - Absent → **Bootstrap**.
  - Present → list logged batches via the backend. If any is `submitted`/`running`,
    **tell the user** and **offer** a status check. If any is `complete`
    (not collected), offer **Collect**.

### 2. Bootstrap (first use in a repo)
Confirm the **AWS profile** and **cluster name** with the user (the rest has
sensible defaults), then call the backend's `configure` verb. It validates the
profile (`sts get-caller-identity`), discovers the head node by tag, checks the
S3 buckets and the Slurm partition, and writes `config.yml` (gitignoring
`.aws-launch/`). Idempotent with `--force`. If the profile is missing, point the
user at `reference/credentials.md` (the backend can print the exact profile stub).

### 3. Pre-flight (once per new cluster/repo)
Read-only. **Wake the head node first** — it may be stopped. Verify: partition
exists (`sinfo`), the sim binary is built or the build command works, the S3
trace prefix has objects, and the **EC2 Spot service-linked role exists** (its
absence blocks every Spot launch — see operational-notes). Mismatches here are
the usual cause of a failed first submit.

### 4. Submit a batch
Call the backend's `submit` verb with the job spec (a trace list × an experiment
list). The backend, in order: **wakes** the head node, ensures the binary is
built, **smoke-gates** (runs one quick job; proceeds only if it passes), submits
the sbatch array to the Spot queue (each job stages its input S3→node-local NVMe
`/scratch`, runs, pushes its result→S3), and records `tag → job_id` in the
batch **ledger**.
- Success → report batch id + job count.
- Failure → relay the backend's `error_id` + reason; **no** jobs queued.
- Submits can take minutes (Spot boot + build + smoke) — **run in the background**.
- A submit that scales **many** Spot nodes for the first time in a session →
  flag the scale-out and get the user's ok first.

### 5. Check status
Call the backend's `status` verb — wakes the head node if needed, queries
`squeue`/`sacct` over SSM, updates the ledger, and reports per-state counts plus
the current **Spot node count** (the elasticity readout). A batch becomes
`complete` when every job is terminal. **Poll only while the user is present**;
offer an opt-in loop (e.g. `/loop 10m`) if they want to wait.

### 6. Collect
When a batch is `complete`, call the backend's `collect` verb — it fetches each
job's result from S3, builds a local summary table (e.g. IPC per trace/exp),
and writes it to `<repo>/.aws-launch/runs/<batch>/`. Refuses an incomplete batch
unless `--force`.

The sync is incremental, and same-region reads are free (egress to a laptop is
$0.09/GB with 100 GB/month free). **If the raw outputs are large, run the rollup on
the head node and copy out only the summary** — and check one job's output size before
launching hundreds: a debug knob left on in a shipped config once made each result 37 MB
instead of 0.3 MB. See "Collecting results" in `reference/operational-notes.md`.

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
- `submit`/`collect` **mutate** (S3 writes, job submission); `status`/`list` are
  read-only. Be explicit about which you're about to run.
- Region is **locked** (config `region`); every AWS call is scoped to it.
