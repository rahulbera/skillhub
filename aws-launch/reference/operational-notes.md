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

    s3://<results>/<owner>/<project>/bootstrap/   wrapper, launch scripts, batch inputs, submit reports
    s3://<results>/<owner>/<project>/results/     wrapper-route results; jobfile-route stats.csv + rollup_report.json
    <remote_project_root>/
      Hermes/              that project's ChampSim checkout + built binary (synced)
      champsim-infra/      create_jobfile/rollup scripts (synced; jobfile route)
      results/             wrapper route: Slurm .out/.err logs only
      results/<batch>/     jobfile route: the batch's snapshot (bin/, snapshot_dirs, scripts/, inputs/) + every job's .out/.err
      run-assets/<batch>/  wrapper route: the batch's snapshot (bin/, snapshot_dirs) + wrapper; submission happens HERE, never in $HOME

**Reserved: `s3://<results>/bootstrap/` is CLUSTER INFRASTRUCTURE, not project data.**
The ParallelCluster config's `OnNodeConfigured` script lives there and is fetched by every
compute node at boot. It is deliberately outside the per-owner namespace because it belongs
to the cluster, not a person. Do not move, rename, or "tidy" it into an owner prefix: nodes
fail bootstrap with a 404, clustermgtd terminates them, and after 10 failures ParallelCluster
puts the whole cluster into PROTECTED mode and marks the partition INACTIVE. That does not
self-heal -- recovery is `pcluster update-compute-fleet --status START_REQUESTED` after the
key is restored. (This happened on 2026-09-07.)

`project` is deliberately OWNER-FIRST (`rbera/hermes-uncore`), so everything one person
owns sits under a single top-level prefix. That is what makes the IAM policy trivial --
one statement, `<results-bucket>/<owner>/*`, grants read+write to exactly that person's
space and nothing else. The **traces bucket is shared and READ-ONLY** for everyone but its
owner: it is ~1 TB that took hours to populate, and no job ever needs to write there.

This is not cosmetic. Before namespacing there was a single
`bootstrap/champsim-job.sh` and submission ran in `$HOME`, so a second project or a
second person submitting would overwrite the first one's wrapper and scratch files
mid-flight. `load_cfg` now refuses to run if a config lacks these keys rather than
silently falling back to the shared layout.

Two details worth knowing:
- The bundled `scripts/champsim-job.sh` is a **template**. Its `#SBATCH -o/-e` lines
  cannot use a shell variable (Slurm parses them before any shell runs), so the backend
  substitutes `{{PROJECT_ROOT}}` at upload time, per project, and `{{BINARY}}` per batch.
- Wrapper-route results are NOT kept on the head node. A job writes stats to the compute
  node's `/scratch`, uploads to S3, and deletes the local copy; only the small Slurm logs stay
  on NFS. Keeping a second copy on the head node grew /home by ~14 GB per campaign on a
  97 GB volume. The jobfile route is the exception: each job's `.out`/`.err` stays in
  `results/<batch>/`, because rollup reads it there. Check one `.out`'s size before a large
  campaign (below), and remove a collected run dir once its `stats.csv` is safe.

## Collecting results — and what to do when the outputs are large

The wrapper route's `collect` runs `aws s3 sync` from `s3://<results>/<owner>/<project>/results/`
to the local run dir. The sync is **incremental**, so re-running a rollup re-downloads nothing.

Cost, so you can reason about it rather than guess:
- Reading S3 from **inside** the region (head node, compute nodes) is **$0.00/GB**.
- Pulling to a laptop is internet egress at **$0.09/GB**, but the first **100 GB/month**
  is free account-wide. GET requests are ~$0.0004/1000 — noise.

So for a normal campaign (results measured in MB) just `collect` and forget it.

**When results are large (multi-GB), do the rollup ON the head node** — same-region
reads are free and there is no egress — then copy out only the summary CSV. Sending
10 GB of raw `.out` files to a laptop to compute a few hundred numbers is the wrong
shape; ship the computation to the data. The jobfile route's `collect` does exactly this:
it runs the batch's own `rollup.py` in `results/<batch>/` and brings back only `stats.csv`
and `rollup_report.json`.

### Check output size before a long campaign — a debug knob can dominate it

Look at what one job actually produced before launching hundreds:

```bash
aws s3 ls s3://<results>/results/<project>/ --recursive | sort -k3 -n | tail -3
```

Real example worth internalising. `config/pythia.ini` ships
`scooby_enable_state_action_stats = true` (the knob's own default in `knobs.def` is
`false`). It dumps the prefetcher's entire per-state action table at end of run: **1.84M
lines, ~37 MB per Pythia job, 99% of the file**, versus ~0.3 MB for the same run with it
off. One 714-job campaign wrote **13.1 GB** of results, of which ~13 GB was that table —
and nothing in the rollup ever read it. Overriding it on the command line
(`--scooby_enable_state_action_stats=false`, placed AFTER the `--config` that sets it)
cut results ~360x with zero effect on any reported metric.

The general rule: a config shipped for a paper's debugging is not automatically the right
config for a bulk sweep. Diff the per-file size of one job against what you expect, and
if a single result is tens of MB, find out what is in it before multiplying by N.

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
multi-TB ext4 volume on the instance store. The jobfile route does the same through
champsim-infra's `run_champsim.py`/`fetch_trace.py`, caching each trace under
`trace_cache_dir` (default `/scratch/trace_cache`) with a per-trace lock. The head node
has no instance store: `submit` creates `/scratch` there for `remote_user`, and the
smoke's trace stays cached on the root volume.

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
- **Source syncs and builds:** `sync --build` mirrors the local tree (sha256-verified)
  and `build_command` produces `binary`. This step mutates: S3 upload, remote files.
- **Inputs resolve:** `aws s3 ls s3://<traces>/<trace_prefix>/` returns objects.
- **EC2 Spot service-linked role exists** (see "Why a command failed").
- **Jobfile route:** the head node's `python3` imports `yaml`, and `sync:` mirrors
  `champsim-infra` to `remote_infra_path`.

## Smoke-gate before the full array
Always run ONE quick job first; submit the array only if it passes. A bad build,
a wrong knob, or a missing `--trace_version=2` fails the smoke job cheaply instead
of failing (and paying Spot boot for) a whole array. `submit` owns this gate, and runs
it from the batch's own snapshot on the head node. Each gate's result is keyed per
attempt (`hsmoke_<exp>.<attempt>`, `smoke_<exp>.<attempt>`), so a result left by an
earlier attempt cannot pass a later gate.

## Source sync — why every submit mirrors and rebuilds
The head node is long-lived, and its checkout is whatever was last put there. On 2026-09-10 one
project's copy was a GitHub clone from a week earlier, six commits behind the local tree and missing
a knob two experiment arms depended on. The old `submit` built only when no binary existed, so it
would have smoke-tested and launched that stale binary. That binary silently ignored unknown knobs:
both arms would have run identical code, and every job would have succeeded.

So `submit` syncs first. The transport is S3, since the head node has no SSH. rsync alone is not
trusted — its quick check compares only size and mtime — so the head node recomputes every sha256
against the shipped manifest and the backend compares each tree's digest with the local one.
- **What ships** is git's view of the tree on disk: tracked files plus untracked files not ignored.
  Gitignored build products never ship; list the ones built on the head node (`libbf`, `bin`, `obj`)
  under `protect`, and the mirror neither copies nor deletes them. `exclude` stops a path shipping,
  and any head-node copy of it is then deleted like any other stray file.
- **Everything else under a synced directory is deleted**, a leftover `.git` included. A remote may
  never be `results/` or `run-assets/`, and a destination that is or passes through a symlink out of
  the project root is refused: rsync --delete would otherwise mirror into, and delete from, the link's
  target on a shared node.
- **Opt-in.** A config without `sync:` refuses and prints a proposed entry. Configs written before
  sync existed therefore do not start mirroring with --delete on their next submit.
- **Safe under running work.** Jobs read their binary, `--config=` files and wrapper scripts when they
  start, not when they are submitted — so every batch runs from its own snapshot (next section), and
  `sync`/`submit` never wait for the queue.
- **One lock spans mirror, verify and build**, so two syncs of one project cannot interleave, and no
  tree changes between its check and its build.
- **Only a binary newer than the run counts as built.** The old one stays in place until the build
  replaces it, so a failed or no-op build is `build_failed` instead of passing `test -x` with a stale
  binary.
- **Provenance** — git HEAD, uncommitted changes, per-tree digest, binary sha256 — lands in
  `.aws-launch/sync/<stamp>/summary.json` and in the batch ledger.

## Self-contained batches — why a sync can run under queued jobs
Before this, jobs read `--config=<remote_repo_path>/config/*.ini` and the wrapper scripts from the live
tree when they started, and the bundled wrapper ran the live binary. create_jobfile already hardlinked
the binary per batch, but a sync while a batch was pending still silently reconfigured its unstarted
jobs. Now a batch directory holds everything its jobs read, taken inside the sync lock right after the
build:

    results/<batch>/        jobfile route          run-assets/<batch>/     wrapper route
      bin/<exe>.<stamp>       copy of the binary     bin/<exe>.<stamp>       copy of the binary
      config/                 cp -a (snapshot_dirs)  config/                 cp -a (snapshot_dirs)
      scripts/                cp -a of infra scripts champsim-job.sh         rendered to run bin/
      inputs/                 tlist, exp, <exp>.resolved.yml, mfile

- **The binary is a copy.** It was first a hard link, which stays frozen only while every later build
  replaces the binary by rename; a build that writes it in place (`cp build/champsim bin/…`) would have
  changed every earlier batch's binary with no error anywhere. A copy (`cp -p` to a temp name, then `mv`)
  costs one binary per batch. create_jobfile runs it as given (`--no-snapshot-exe`), so the live binary is
  never linked.
- **Frozen under the sync lock, then checked.** The first version copied in a separate SSM command after
  the lock was released, so a second submit's sync waiting on that lock could mirror its tree into the
  first batch's copy. Now the copy is the last step of the locked command, and it is verified: the
  copied binary must hash to what this run built, and each copied dir must match the sync manifest, or
  the submit stops (`snapshot_mismatch`) before anything runs from it. With `--no-sync` the copy still
  waits for the lock, and the ledger's snapshot note says there were no digests to compare.
- **No symlink out of the batch.** `cp -a` keeps symlinks, and one leading outside the batch dir (an
  absolute link into the live tree, say) would pass the manifest check yet still read the live file. Such
  a link is `snapshot_failed`; replace it with the file.
- **Which dirs are frozen is per repo.** `snapshot_dirs` (default `[config]`) lists them under
  `remote_repo_path`; a ChampSim tree with runtime TOMLs in `configs/` lists `configs`. A listed dir the
  head node lacks is skipped with a warning.
- **Paths are expanded as create_jobfile will, then rewritten; anything else live is refused.** The
  jobfile route expands each exp's `$(VAR)`s with that file's definitions in create_jobfile's single
  pass, so a live path split across a definition (`$(ROOT)/Hermes/config`) is seen whole, and a `$(VAR)`
  the pass leaves is `unresolved_variable` (a job's shell would run it). Paths into `snapshot_dirs`
  become the batch's copy in `<exp>.resolved.yml`, which keeps no definitions and must load back to exactly
  those experiments (`bad_input`); the authored file is kept. Each path-like token is normalized first:
  `//` and `/./` collapsed, `..` resolved (a relative token from the dir its jobs start in, the batch
  dir), `$HOME`, `${HOME}` and `~` expanded to `/home/<remote_user>`. A definition that ends in `/` once
  turned `$(ROOT)/Hermes/config` into `<root>//Hermes/config`, which the literal match missed while the
  kernel read the live file. A token that names `remote_repo_path` or `remote_infra_path` only once
  normalized is refused, with its canonical spelling, and never rewritten. Any remaining whole-path
  reference to `remote_repo_path`, `{REMOTE_REPO}` or `remote_infra_path`, on either route, is refused
  before the head node wakes (`live_tree_reference`); a sibling such as `<remote_repo_path>2` or
  `<remote_repo_path>+2` is another tree. Trace paths are not rewritten.
- **Everything runs from the batch.** create_jobfile, `run_champsim.py` and `rollup.py` run from
  `results/<batch>/scripts/`; the wrapper route submits from `run-assets/<batch>/` with a wrapper
  uploaded per batch, so two submits of one project cannot swap wrappers.
- **A label is single-use.** An existing batch dir is refused (`batch_exists`, `run_dir_exists`), and so
  is an existing local `runs/<batch>/`: an attempt writes `attempt.json` there once its batch is frozen,
  since from then on it may queue jobs. A running submit also holds `runs/<batch>.claim` from before the
  wake until it records its attempt or exits, so two submits of one label, even on different routes,
  cannot both freeze a batch and race to record it. Nothing of a failed attempt is deleted, locally, in S3
  (its job-id record and report sit under per-attempt keys) or on the head node.
- **Batches written before this** (wrapper ledgers without a snapshot) still `status` and `collect`
  through the old path.

## SSM truncates stdout at 24,000 characters — never parse a long list from it
`aws ssm send-command` caps `StandardOutputContent` at **24,000 characters** and drops
the rest **without an error**. Anything that returns one line per job overflows at
roughly **340 jobs** and the tail vanishes silently.

This bit a real 450-job batch: `submit` parsed `SUBMIT <exp> <trace> <jobid>` lines
from stdout, recorded **335**, wrote that ledger, and printed
`OK — batch full_512kb: 335 jobs queued` with exit 0. All 450 jobs were in fact
queued and running; only the bookkeeping was short. Because `collect` reads the
ledger, 115 finished jobs would have been skipped and the analysis would have been
built on two-thirds of one experiment, with nothing anywhere reporting a problem.

The rule: **any per-job record travels through S3, not SSM stdout.** The remote
submitter writes `submitted.txt` and uploads it to
`s3://<results>/<owner>/<project>/bootstrap/<batch>.<attempt>.submitted.txt`. The key is per attempt,
and the record opens with `ATTEMPT <attempt>` and closes with `SUBMIT_COMPLETE <n>`: an earlier attempt's
record of the same label once passed for a new attempt's, so a record without both is not read. The backend
reads that file, keeps it beside the ledger, and then **asserts the count equals `traces x exps`**, exiting with
`error_id=ledger_incomplete` if not (the ledger keeps the ids it got). The jobfile route follows the same
rule: create_jobfile's `report.json` comes back through S3 and its job count is asserted against
`num_pairs`. Neither route falls back to stdout, and both records are uploaded and fetched with retries:
a record that never reaches S3 is `submit_no_record` or `jobfile_no_report`, whose message names the
head-node copy to check alongside `squeue`. Bracket
any other long listing with sentinels and
verify the closing one arrived — `status` does this with `QSTART`/`QEND` and refuses to
update the ledger if the end marker is missing, because a truncated queue listing reads
as "those jobs finished".

## SSM stops a command at its executionTimeout — but may miss a `runuser -c` launch
`send-command`'s `TimeoutSeconds` bounds only delivery; `AWS-RunShellScript` kills a running command at
its `executionTimeout` parameter, 3600 s by default, however long the caller waits. So every backend
command passes its own timeout as `executionTimeout`, and a launch gets `launch_timeout_s` (default 7200;
raise it for a very large batch). That kill reaches the command's process group, but `runuser -l <user> -c`
starts a new session, so a launch can outlive it and keep calling sbatch after the backend has given up.
The launch therefore bounds itself: `timeout -k 60` stops create_jobfile (jobfile route) or
`_submit_remote.sh` (wrapper route), and every sbatch it started, 300 s before `launch_timeout_s`. A launch
stopped by that bound is `jobfile_timeout` (wrapper route: `submit_no_record`). Jobs it queued before the
bound MAY be queued: check `squeue` and `<run>/create_jobfile.log`, one `[submit] <tag> -> job <id>` line
per job it queued (wrapper route: `run-assets/<batch>/submitted.txt`).

A sync waits up to 15 min for the project lock, and its command's timeout runs past that wait, so a lock
held too long is `sync_locked` (nothing frozen). A `sync_timeout` during a submit may still freeze its batch
dir, which spends the label although nothing is queued from it.

## The ledger is the source of truth
`submit` captures `tag → job_id` into `<repo>/.aws-launch/runs/<batch>/ledger.json`, and every ledger
write (submit, status, collect) is atomic: a temp file in the same dir, fsync, rename. `status` and
`collect` read it; never reconstruct job ids by hand. A submit that ends before it freezes its batch
queued nothing, and its label is free again unless it was killed hard (see Known limitations). From the freeze on, `runs/<batch>/attempt.json` exists, and a
submit that was killed or interrupted after that counts as one that MAY have queued jobs, like the errors
that say so: `jobfile_timeout`, `jobfile_no_report`, `jobfile_already_launched`, `submit_no_record`,
`smoke_timeout` and, when its message says so, `smoke_submit_failed`. Check `squeue` and the head-node record
first. `status` tries a read-only recovery of such an attempt from its own S3 record and writes the ledger
if the record is there; otherwise it lists the attempt as unknown, and `status --batch <batch>` exits
`batch_unknown`. `submit_partial` and `ledger_incomplete` write a ledger of the jobs that did queue.

## Read-only vs mutating
`status` reads the queue but wakes the head node first. `sync` (S3 writes, remote
files), `submit` (the same, plus job submission) and `collect` (S3 reads + local
writes) change state. `submit` also causes **Spot instances to launch** — a first
large scale-out in a session is worth flagging to the user.

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
| `AccessDenied` on the `AssumeRole` operation | The profile's `role_arn` is wrong, or the user was never allowed to assume it. See `credentials.md`. |
| `AccessDenied` on `PutObject` under the results bucket (a cluster user) | The owner part of `project` is not the user's own name; a `ChampSimRunner<Name>` role writes only under `<results-bucket>/<name>/`. |
| `AccessDenied` on `StopInstances` / `TerminateInstances` / CloudFormation (a cluster user) | Intentional; runner roles cannot change the cluster. Only the cluster owner (`ParallelClusterDeployer`) can. |
| Sim crashes `va_to_pa Assertion 0` on a `.champsim2.zst` trace | v2 trace read as v1 — add `--trace_version=2` to the knobs. |
| STS/credential error mid-run | 1 h token expired; it auto-refreshes from the source profile — just retry. |
| `submit` refuses with `live_tree_reference` | An experiment, once its `$(VAR)`s are expanded and its paths normalized, points into `remote_repo_path` outside `snapshot_dirs`, or into `remote_infra_path`; queued jobs would read it live. Move the file under a `snapshot_dirs` entry, add its dir to `snapshot_dirs`, drop the reference, or write a path into `snapshot_dirs` in the canonical spelling the message shows. |
| `submit` refuses with `unresolved_variable` | An exp uses a `$(VAR)` its own file does not define, or one create_jobfile's single expansion pass leaves literal (a definition using another). Define it in that file, or inline the inner value. |
| `submit` refuses with `batch_exists` / `run_dir_exists` | The label was used before, possibly by a failed attempt that may have queued jobs, or another submit of it is running (`runs/<batch>.claim`). Check `squeue`, then pick a new `--label`. |
| `submit` stops with `snapshot_mismatch` | Something wrote to the batch's binary or copied dirs without the sync lock (a manual build or copy on the head node). The batch dir stays as evidence; find the writer, then submit under a new label. |
| `submit` stops with `snapshot_failed` naming a symlink | A copied dir holds a symlink that leads out of the batch, so its jobs would read the live file. Replace the link with the file, then submit under a new label. |
| `jobfile_timeout`, `jobfile_no_report`, `jobfile_already_launched`, `submit_no_record` | The launch stopped at its own bound, or the job-id record never reached S3. Jobs MAY be queued: check `squeue` and the head-node record the message names before resubmitting; `status --batch <batch>` writes the ledger if the record reaches S3. |
| `smoke_timeout`, or `smoke_submit_failed` saying the gate job MAY be queued | The `--spot-smoke` gate job may still be queued: `scancel` the id the message names (or find it in `squeue`). |
| `status` lists `state=unknown`, or exits `batch_unknown` | A submit froze its batch but wrote no ledger, and its S3 record is not there (yet). Jobs MAY be queued: check `squeue` before resubmitting. |

## Known limitations

Found in review and left open. Each entry says how it fails.

- **A label ending in `.claim` collides with the label claim file** `runs/<label>.claim`. Don't use
  one: a `batch_exists` hint could then point at deleting another batch's record.
- **A hard-killed submit leaves its claim.** SIGTERM or SIGKILL before the batch freezes skips the
  cleanup, so `runs/<batch>.claim` blocks the label until you delete it. Nothing was queued.
- **`smoke_submit_failed` can miss a queued gate job.** When sbatch prints an id the backend cannot
  parse (e.g. multi-cluster `4242;cluster1`), it says nothing was queued. Check `squeue`.
- **Any rc 137 from the jobfile launch reads as `jobfile_timeout`,** including an out-of-memory
  kill long before `launch_timeout_s`. Jobs are still reported as MAY be queued.
- **Some refusal hints dead-end.** `status --batch` cannot recover a run dir without
  `attempt.json` (older versions). `collect` says a batch whose report shows nothing queued MAY
  have queued jobs, where `status` calls it failed.
- **The live-tree refusal resolves only the spellings SKILL.md lists.** `$USER`, `$PWD`,
  `$SLURM_SUBMIT_DIR` and other variables pass through and are expanded by the job's shell.
- **Wrapper-route `collect` finds no results.** It looks for `<stem>-<exp>-j<jid>.txt`, but
  `champsim-job.sh` uploads `results/<exp>/<stem>__<exp>.txt`. This predates self-contained
  batches.
- **Not yet exercised on the real cluster:** `executionTimeout`, the launch bound's kill of a
  `runuser -c` session under the SSM agent, and `cp -pL` on the head node.

## Sandbox / network
SSM and S3 calls need real network. If the local shell is sandboxed, the first
`aws` call fails — run the authorized cluster ops with the sandbox disabled, or
prime `AWS_PROFILE=<p> aws sts get-caller-identity` first. Never assume standing
approval for remote/mutating actions across turns.
