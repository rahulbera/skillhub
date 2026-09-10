# Backend contract (AWS)

A **backend** performs the workload-specific execution for `aws-launch`. The
skill sequences the verbs below and applies judgment; the backend does the actual
wake/sync/build/submit/query/collect over SSM + S3. `config.yml`'s `backend.command`
names the entrypoint. The bundled `scripts/aws_launch.py` is the reference
implementation (ChampSim/Hermes); swap it for another workload by pointing
`backend.command` elsewhere and keeping the same verbs.

Each verb is invoked as `<command> <verb> --repo <repo-root> [args]` and reads
`<repo>/.aws-launch/config.yml`.

## `configure` (bootstrap)
Establish/validate the per-repo config. Effects: validate the AWS profile
(`aws sts get-caller-identity`), discover the head node by cluster tag, check the
S3 buckets + Slurm partition, write `config.yml` (including `remote_infra_path` and
`trace_cache_dir`), and gitignore `.aws-launch/`. Idempotent with `--force`. If the
profile is missing, print the exact `~/.aws/config` stub (see credentials.md) and stop.
A config written before `remote_infra_path`/`trace_cache_dir` existed loads with
`<remote_project_root>/champsim-infra` and `/scratch/trace_cache`.

## `sync`
Make the head node's source identical to the local work trees named in `sync:`, then optionally
build (`--build`). Refuses before touching anything when `sync:` is absent
(`error_id=sync_not_configured`, printing a proposed entry). It never checks the Slurm queue: every
batch runs from its own snapshot (see `submit`), so a sync cannot change a pending or running job.
Otherwise: pack each tree's
source set (tracked + untracked-not-ignored files, as on disk) with a sha256 manifest; upload it to
`s3://<results>/<owner>/<project>/bootstrap/sync/<stamp>/`; then, in ONE SSM command under a
per-project lock, `rsync --delete --checksum` each tree into `<remote_project_root>/<remote>`
(leaving `protect` untouched and refusing a symlinked destination, `sync_unsafe_destination`),
recompute every hash, and run the build. Exits `sync_verify_failed` unless each tree's digest equals
the local one — nothing builds after that — and `build_failed` unless the build produced a binary
newer than this run. Manifests and `summary.json` stay in `.aws-launch/sync/<stamp>/`. The lock wait is
15 min (`sync_locked`), and the command's SSM timeout runs past it (`sync_timeout` otherwise).

## `submit`
Run one batch end-to-end and record it, in exactly one of two input routes (argparse refuses a mix):
- **jobfile** — `--tlist T.. --exp E.. --mfile M.. [--smoke-idx N]`: champsim-infra YAML, launched by
  `create_jobfile.py`.
- **wrapper** — `--traces file --exps "name=knobs;..." [--spot-smoke]`: launched through `job_wrapper`.

Both take `--label L` (letters, digits, `._-`; default `batch-<date>-<time>`) and `--no-sync`. Steps:
1. **Validate locally, before waking**: the label, the config values remote commands interpolate
   (`snapshot_dirs`, `launch_timeout_s` included), and that `runs/<batch>/` does not exist yet
   (`batch_exists`: a label is single-use, and an attempt's record is never deleted). Jobfile route: every
   input exists and parses, basenames are unique, traces × experiments is counted, and each exp is
   resolved (step 4) into `<name>.resolved.yml` beside the authored file. Wrapper route: every `--exps` name
   uses letters, digits and `._+-` (`bad_input_name`) and no trace key holds whitespace (`bad_input`), since
   the job-id record is one `SUBMIT <exp> <trace> <jobid>` line per job. Then the label is claimed with an
   exclusive `runs/<batch>.claim`, held until the attempt is recorded (step 4) or the submit exits; another
   submit of the label, on either route, is `batch_exists`.
2. **Wake** the head node (`scripts/aws-wake.sh`).
3. **Sync and build** exactly as `sync --build` does, and step 4 in the same head-node command under the
   same lock. `--no-sync` skips the mirror; the wrapper route then builds only when no binary exists,
   the jobfile route not at all.
4. **Freeze the batch** right after the build, inside that lock (with `--no-sync`, alone under it), so
   nothing its jobs read at start lives in a synced tree and no other sync can land in between:
   - `bin/<exe>.<stamp>`, a real copy of the binary (never a hard link, whose inode a build writing the
     binary in place would change), and a `cp -a` of each `snapshot_dirs` entry (dirs under
     `remote_repo_path`, default `[config]`; one the head node lacks is skipped with a warning), plus the
     jobfile route's `scripts/` from `remote_infra_path`. They go into `<remote_project_root>/results/<batch>/`
     (jobfile) or `run-assets/<batch>/` (wrapper, whose wrapper is rendered with `{{BINARY}}` set to that
     snapshot and uploaded per batch).
   - The copied binary must hash to what this run built (or, `--no-sync`, found) under the lock, and each
     copied dir inside a synced tree must match that tree's manifest; otherwise `snapshot_mismatch`. A symlink
     in a copied dir that leads out of the batch is `snapshot_failed`. The ledger's `snapshot` says whether it
     was `verified`, and why not (`--no-sync`: no digests to compare).
   - An existing batch dir refuses before anything is mirrored: `batch_exists` (wrapper) or
     `run_dir_exists` (jobfile).
   - `runs/<batch>/attempt.json` then records the attempt, with what `status` needs to recover its ledger;
     if another submit of the label recorded first, this one exits `batch_exists`.

   Jobfile route: each exp's `$(VAR)`s are expanded exactly as create_jobfile does (the file's own
   definitions, one pass) and the result is written without definitions; a `$(VAR)` left over is
   `unresolved_variable`, and a result that does not load back unchanged from YAML is `bad_input`. In both
   routes `{REMOTE_REPO}/<d>` and `<remote_repo_path>/<d>`, for each `snapshot_dirs` entry, point at the
   batch's copy when spelled canonically; any other whole-path reference to `remote_repo_path`,
   `{REMOTE_REPO}` or `remote_infra_path` is `live_tree_reference`, before waking. Each path-like token is
   normalized first — `//` and `/./` collapsed, `..` resolved (a relative token from the directory the jobs
   start in: the run dir, or `run-assets/<batch>/`), `$HOME`, `${HOME}` and `~` expanded to
   `/home/<remote_user>` — and a token that names a live tree only once normalized is refused with its
   canonical spelling, never rewritten. A sibling such as `<remote_repo_path>2` or `<remote_repo_path>+2` is
   another tree. Trace paths are not rewritten.
5. **Smoke-gate** on the head node, from the snapshot; proceed only on a clean result.
   - jobfile: ONE SSM command (as root) gives `remote_user` a `/scratch`, then (as `remote_user`) runs a
     launch script shipped as a file: check the run dir holds this attempt's snapshot, stage `inputs/` (whose
     `mkdir` fails if another launch of this attempt got there first: `jobfile_already_launched`), and run
     `timeout -k 60 <launch_timeout_s − 300> python3 scripts/create_jobfile.py --exe <run_dir>/bin/<exe>.<stamp>
     --no-snapshot-exe --wrapper <run_dir>/scripts/run_champsim.py --trace-cache-dir <trace_cache_dir>
     --slurm-part <partition> --ncores <ncores_per_job> --extra "--requeue --time=<walltime>
     --export=ALL,AWS_DEFAULT_REGION=<region>" --exp inputs/<exp>.resolved.yml.. --tlist inputs/<tlist>.. -o
     jobfile.sh --smoke-test-auto-launch --smoke-test-idx N --smoke-warmup 1000000 --smoke-sim 1000000
     --report-json report.json`, which smoke-tests one pair and only then submits every pair. `report.json`
     and `create_jobfile.log` go, with retries, to
     `s3://<results>/<owner>/<project>/bootstrap/jobfile/<batch>/<attempt>/`. The command may run
     `launch_timeout_s` (SSM's executionTimeout), but SSM's kill need not reach a command under `runuser -c`,
     which starts a new session, so the launch bounds itself; one it stops is `jobfile_timeout`.
   - wrapper: the real wrapper runs on the head node (plus one Spot job with `--spot-smoke`, whose name and
     result key, `smoke_<exp>.<attempt>`, are per attempt and whose id goes into `attempt.json`), then
     `_submit_remote.sh` submits the array from `run-assets/<batch>/` under the same
     `timeout -k 60 <launch_timeout_s − 300>`.
6. **Ledger**: job ids come only from S3 (`report.json`, or the per-attempt
   `<batch>.<attempt>.submitted.txt`, which must open with `ATTEMPT <attempt>` and close with
   `SUBMIT_COMPLETE <n>`), never from SSM stdout: the record is fetched, with retries, whatever stdout says,
   and nothing falls back to stdout. Write `runs/<batch>/ledger.json` with state `submitted`, the sync record
   and the snapshot record.

Output: batch id + job count on success. On failure a stable `error_id` (`live_tree_reference`,
`unresolved_variable`, `build_failed`, `snapshot_mismatch`, `smoke_failed`, `jobfile_failed`,
`run_dir_exists`, ...) with **no** jobs queued and no ledger, except:
- queued and recorded: `submit_partial` (jobfile route: some sbatch calls failed; the launched jobs are in
  the ledger with `partial: true`) and `ledger_incomplete` (fewer ids than pairs; the ledger keeps those).
- MAY be queued, not recorded: `jobfile_timeout`, `jobfile_no_report`, `jobfile_already_launched` and
  `submit_no_record`, each naming the head-node record (`<run_dir>/report.json` and `create_jobfile.log`, or
  `run-assets/<batch>/submitted.txt`) to check with `squeue` before resubmitting; `smoke_timeout` and, when
  its message says so, `smoke_submit_failed`, for the `--spot-smoke` gate job (named for `scancel` when its
  id is known); and a submit killed or interrupted after its batch froze.
Long-running → callers run it in the background.

## `status`
Report progress. Wakes the head node, queries `squeue` (active) + `sacct`
(finished) over SSM, updates the ledger, prints per-state counts and the current
Spot compute-node count. Batch → `complete` when every job is terminal. Works on
either route's ledger. Read-only on cluster *state* (but it does wake the head node).

A run dir with `attempt.json` and no ledger is an attempt that froze its batch and may have queued jobs.
Before waking, `status` tries a read-only recovery from the attempt's own S3 record: `report.json` under its
per-attempt key, or its `<batch>.<attempt>.submitted.txt` (this attempt's token and `SUBMIT_COMPLETE`
required). A record that proves what was queued becomes the ledger, and status carries on; one that proves
nothing was queued is reported as such. Otherwise the attempt is listed as `state=unknown — may have queued
jobs`. Without `--batch` every such dir is handled before the latest ledger's status; `status --batch B` on
one it cannot recover exits `batch_unknown`.

## `collect` (rollup)
Gather a complete batch's results; refuses an incomplete batch unless `--force`, and a batch with no ledger
(`batch_unknown`: `status --batch B` tries to recover it).
- **jobfile ledger** (`mode: jobfile`): one SSM command runs `<run_dir>/scripts/rollup.py` on the batch's
  own `inputs/` (the resolved exp) with `-d <run_dir> -o <run_dir>/stats.csv --report-json
  <run_dir>/rollup_report.json` and uploads both to `s3://<results>/<owner>/<project>/results/<batch>/`.
  The backend downloads them into `runs/<batch>/`, prints the summary and the non-ok runs (bounded),
  and flags rows whose `ipc` is 0. Raw `.out`/`.err` never leave the head node. `rollup_failed` if the
  rollup or its upload fails.
- **wrapper ledger**: reads each job's result object from `s3://<results>/<owner>/<project>/results/`,
  builds a local summary table (IPC per trace/exp) in `runs/<batch>/summary.csv`, and flags
  missing results.

## Ledger
The backend owns `runs/<batch>/ledger.json`, the source of truth for `status` and `collect`:
`{batch, mode, state, submitted_at, n_jobs, sync, snapshot, jobs:[{tag, job_id, ...}]}`, where `snapshot`
is `{dir, binary, binary_sha256, snapshot_dirs, copied, skipped, verified, note}`. `attempt.json` beside it
marks an attempt that froze its batch, ledger or not, and keeps what recovery needs: the attempt `token`,
its S3 record (`s3` or `record`), the staged `inputs` or `n_expected`, and the gate's `smoke_job_id`. Both
are written atomically (temp file in the same dir, fsync, rename), so an interrupted write keeps the old one.
- `mode: wrapper` — jobs also carry `trace` and `exp`; `run_assets` names the batch snapshot; `incomplete`
  marks a ledger with fewer ids than pairs; the fetched `submitted.txt` sits beside the ledger. A ledger
  without `mode` predates it and is a wrapper ledger.
- `mode: jobfile` — `remote_run_dir`, `inputs` (staged names), `num_pairs`, `partial`, `exe`, `smoke`,
  `s3`. `report.json`, `create_jobfile.log`, `launch.sh` and `inputs/` sit beside the ledger; `collect`
  adds `stats.csv` and `rollup_report.json`.

## Lifecycle primitives (shared)
- `scripts/aws-wake.sh` — idempotent head-node wake (start → SSM online →
  slurmctld ready). Every mutating/querying verb calls it first.
- `scripts/deploy-idle-stop.sh` — installs the head-node auto-stop timer
  (`idle_stop_minutes` + active-session guard). Run once at cluster setup.
- `scripts/champsim-job.sh` — the reference per-job wrapper (S3 stage-in →
  run → S3 stage-out); the backend renders it per batch and uploads it to
  `s3://<results>/<owner>/<project>/bootstrap/<batch>.champsim-job.sh`.
