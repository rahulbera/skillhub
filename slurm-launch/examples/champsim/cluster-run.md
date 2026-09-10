# Cluster-run: launching ChampSim sims on a remote Slurm cluster

Run ChampSim-based simulators (Hermes / Pythia / arishem / …) on an SSH-only Slurm
cluster from your local machine, in one shot: sync → build → smoke-gate → launch →
log → status → rollup. Built because the cluster login node bars AI agents, so all
cluster access is over SSH from the local machine; worker nodes aren't reachable.

## Pieces

| Where | What |
|-------|------|
| `champsim-infra/scripts/cluster_run.py` | local orchestrator (`bootstrap`/`submit`/`status`/`rollup`/`list`). Runs locally; does all the SSH/rsync. |
| `~/.claude/skills/cluster-run/SKILL.md` | the Claude Code skill that drives it (global — triggers from any repo). |
| `<sim-repo>/.cluster-run/` | per-repo state (gitignored): `config.yml` + `runs/<batch>.json` ledger + fetched `runs/<batch>/stats.csv`. |
| `create_jobfile.py --smoke-test-auto-launch --report-json` | smoke-gates the batch on the login node, then submits each `sbatch` itself, capturing exact `tag→job_id`, and reports JSON. |
| `rollup.py --report-json` | rolls up stats on the cluster; structured per-run status. |

The orchestrator runs `create_jobfile.py` / `rollup.py` **on the cluster**, from the
batch's own copy of champsim-infra's `scripts/` (champsim-infra is rsynced up too). Jobs
use `--no-trace-cache` (cluster NFS handles concurrent reads). All script changes are
additive/behind flags, so `regression/run_regression.py` is unaffected.

## Usage

```bash
ORCH=/home/rahbera/thesis/champsim-infra/scripts/cluster_run.py   # local python3.12

# 1. once per simulator repo
python3.12 $ORCH bootstrap --repo <sim-repo> \
    --remote-sim-path <cluster path for the sim> \
    --build-command '<build cmd run in that dir>' \
    --cluster <ssh-alias> [--slurm-part <p>] [--remote-python <py>]

# 2. sync + build + smoke-gate + launch
python3.12 $ORCH submit --repo <sim-repo> \
    --tlist t.yml --exp e.yml --mfile m.yml [--label NAME]

# 3. check (squeue + sacct); auto-surfaced on resume
python3.12 $ORCH status --repo <sim-repo>

# 4. when complete: rollup on cluster, fetch stats.csv back, diff vs previous
python3.12 $ORCH rollup --repo <sim-repo> --batch <id>
```

The local source files are never mutated; `submit` stages the tlist/exp/mfile into
`<run_dir>/inputs/`, rsyncs the sim + champsim-infra, builds, snapshots the sim's
`snapshot_dirs` (default `config/`) and champsim-infra's `scripts/` into `<run_dir>/`,
smoke-tests pair 0 on the login node, and only on pass submits every job. A failed
smoke/build launches nothing.

### `$(SIM_HOME_IN_CLUSTER)` placeholder

Trace paths in the tlist are already cluster-absolute, but exp files often reference
files **inside** the sim tree (e.g. `--config=$(SIM_HOME_IN_CLUSTER)/config/x.ini`),
whose cluster-absolute path isn't known until rsync. Write that literal placeholder; at
submit time the orchestrator resolves it to `remote_sim_path` on temp copies before
staging (your source files stay untouched). Other `$(...)` tokens are left for
`create_jobfile`/`rollup` to resolve from their own `definitions`. In exp files, every
canonically spelled path under a `snapshot_dirs` entry (`<remote_sim_path>/config` by
default) is then repointed at the batch's own snapshot (next section).

### Self-contained batches

Jobs read their binary, `--config` files and scripts when they **start**, which for a
queued job can be after a later `submit` has rsynced (`--delete`) and rebuilt the live
trees. So each batch's run dir holds everything its jobs read at run time:

| `<run_dir>/` | Holds |
|---|---|
| `bin/<exe>.<ts>` | copy of the freshly built binary, which the jobfile runs (`create_jobfile.py --snapshot-exe`, the default); a copy, not a hardlink, so a build step that writes the binary in place can't change it |
| `config/` (one dir per `snapshot_dirs` entry) | `cp -a` of `<remote_sim_path>/<dir>`, taken after the build; an entry the cluster's sim tree lacks is skipped with a warning, unless an exp was repointed into it, which fails the submit before `create_jobfile` |
| `scripts/` | `cp -a` of `<remote_infra_path>/scripts`, taken after the build; `create_jobfile.py` and `rollup.py` run from here |
| `inputs/` | the staged tlist/exp/mfile; each exp's `<remote_sim_path>/<dir>` paths point at `<run_dir>/<dir>` (tlist paths are not rewritten) |

- **Submitting while other batches of the same repo are queued or running is safe.**
  Submits themselves share the live trees while they sync, build and snapshot, so those
  of one checkout serialize automatically: each holds `.cluster-run/submit.lock` from
  before it picks its batch id until `create_jobfile` returns, and one that has to wait
  logs `waiting for another submit of this checkout`. Separate local checkouts of the
  same sim share its remote tree and must not submit concurrently.
- **A batch id is claimed before any remote work**: `runs/<batch>.json` is created
  exclusively, as a placeholder with `status: submitting`, and the run dir with a plain
  `mkdir`, so a second submit that picks the same id (same second and label, e.g. from
  another checkout) fails before copying anything. A failure before `create_jobfile`
  runs, or one it reports without launching, removes the placeholder. If
  `create_jobfile` returns no JSON report, the batch MAY have queued jobs: `submit` says
  so and keeps the placeholder, which `status` lists and `rollup`/`combine` refuse;
  check `squeue`, then delete it.
- **`snapshot_dirs`** in `.cluster-run/config.yml` lists the sim dirs, relative to
  `remote_sim_path`, holding files jobs read at start (default `[config]`). A ChampSim
  tree whose runtime TOMLs live in `configs/` (its `config/` is the Python build
  generator) sets `snapshot_dirs: [configs]`. An entry may not start with `bin`,
  `inputs` or `scripts`, which the run dir already uses.
- **An exp that still reads `<remote_sim_path>` is refused** before anything changes on
  the cluster, listing each offending line: a path under it outside `snapshot_dirs`
  (e.g. a knob reading `$(SIM_HOME_IN_CLUSTER)/data/w.bin`; add the dir to
  `snapshot_dirs`), the sim path as the tail of a longer one (such as a mount alias), or
  a spelling that resolves to it: `//`, `/./`, `..` (a relative path resolves from the
  run dir, the jobs' cwd), `$HOME`, `${HOME}` or `~` (the cluster's `$HOME`, read over
  SSH only when an exp uses one). Each experiment is checked again with its own
  definitions substituted, so a path split across definitions is caught too. Only the
  canonical `$(SIM_HOME_IN_CLUSTER)/<dir>/…` spelling is repointed.
- **A copied symlink that resolves outside the run dir fails the submit** before
  `create_jobfile`, since jobs would still read the live tree through it; links that
  stay inside the run dir are kept.
- **`rollup` runs the batch's own `scripts/rollup.py`** over its own `inputs/`, so a
  `rollup.py` fix reaches no existing batch through a sync or `submit`; to re-roll a
  finished batch with it, rsync the file into `<run_dir>/scripts/`. Ledgers from before
  snapshots (no `snapshot` field) roll up with the live
  `<remote_infra_path>/scripts/rollup.py`.
- **`combine`** runs the newest combined batch's `rollup.py` (largest batch id): its
  snapshot if it has one, else the live infra copy — newer code reads older batches'
  outputs. Each batch's staged exps name its own snapshot, so `combine` merges copies,
  `<name>/inputs/<batch>__<exp>`, with every `<run_dir>/<dir>` mapped back to
  `<remote_sim_path>/<dir>`: a definition repeated across batches merges, and a real
  conflict still aborts. `--out-name` may not be an existing batch id, whose `inputs/`
  and `stats.csv` it would replace.
- The ledger records `self_contained` and `snapshot`: `scripts` plus each copied
  `snapshot_dirs` entry (by default `{config, scripts}`).
  `--no-snapshot-exe` makes jobs run the live binary: `submit` warns and records
  `self_contained: false`.

## Caveats / lessons (the things that bite)

1. **Verify the Slurm partition on a new cluster.** The default `compute` is often wrong.
   `ssh <cluster> "sinfo -h -o '%P'"` — the default is marked `*`. Set `slurm.partition`.
2. **Verify the remote python.** `create_jobfile.py` needs ≥3.9 (`BooleanOptionalAction`)
   + pyyaml. `python3.12` may be absent; pick what's there and set `remote_python`.
3. **YAML rejects tabs.** tlist/exp/mfile must be space-indented; a tab-indented file
   fails with `found character '\t' that cannot start any token` and crashes
   `create_jobfile`/`rollup` on the cluster. Convert leading tabs → spaces.
4. **SSH/rsync need real network.** Under a sandboxed shell, run the orchestrator with
   the sandbox disabled (authorized cluster ops) or prime `ssh <cluster> true` first.
5. **Long submits:** build + smoke take minutes — run `submit` in the background so a
   foreground timeout can't kill it mid-build (which would leave only a `submitting`
   placeholder ledger). Background submits of one checkout wait for each other.
6. **Partial submits aren't orphaned:** if some `sbatch` succeed and some fail, the
   launched jobs are still recorded (`status: partial`) so you can track/cancel them.

### Known limitations of the self-contained-batch checks

Found in review and left open. Each entry says how it fails.

- **`combine --out-name` is compared as a raw string.** `./<batch>`, `<batch>/` or
  `x/../<batch>` passes the batch-id check. The upload then `rsync --delete`s that
  batch's staged `inputs/` and overwrites its local `stats.csv`. Pass a plain new name.
- **The live-tree refusal is textual.** Quoted pieces (`"$HOME"/Hermes/config/...`),
  `${HOME%/}`, `$USER`, `$PWD` and backslash forms get through, and the job's shell opens
  the live file. Write sim paths as `$(SIM_HOME_IN_CLUSTER)/...`.
- **`combine` aborts for labels containing `=` or `:`.** Snapshot paths are mapped back
  per token, so such a batch's exps keep their run-dir paths and rollup reports a
  definition conflict. Keep labels to letters, digits, `.`, `_` and `-`.
- **A binary `create_jobfile.py` cannot copy** (unreadable, disk full) stops it with no
  JSON report, and `submit` then says the batch MAY have queued jobs, although none were.
- **A submit killed before `create_jobfile.py` runs** leaves its `submitting`
  placeholder, so `status` says it MAY have queued jobs, although none were.
- **`--out-name` is checked only against this checkout's ledgers.** A batch another
  checkout submitted into the same runs base is not protected.

## kratos2 specifics (first real run: 2026-06-20, Hermes)

- Cluster ssh alias `kratos2` (kratos2.ethz.ch); key auth, direct.
- Slurm partition: **`cpu_part`** (default, 19 nodes, 1-day limit). Not `compute`.
- Remote python: **`python3.10`** (3.10.6; has BooleanOptionalAction + pyyaml 5.4.1).
- Hermes cluster home used: `/home/rahbera/from-rnadig/thesis`; traces under
  `/mnt/panzer/rahbera/pythia-dev/ChampSim/dpc3_traces/` (v1 `.xz`).
- First trial: batch `20260620T111937Z_trial`, 10 traces × 3 experiments = 30 jobs,
  smoke-gated and launched on `cpu_part`. Config in `Hermes/.cluster-run/config.yml`.

## Persistence across sessions

- The **skill** (`~/.claude/skills/cluster-run/`) is global — works from any repo.
- Per-repo **config + ledger** live in `<sim-repo>/.cluster-run/`, so a fresh session in
  that repo picks up running batches via `list`/`status`.
- This runbook + the skill are the cross-session record; Claude's per-project *memory* is
  keyed to the working directory and does NOT carry between, e.g., champsim-infra and
  Hermes sessions.

## Tests

`champsim-infra/tests/test_reports.py` and `test_cluster_run.py` (run with `python3.12`;
no pytest needed). They cover the report/error-id additions, the smoke-gate, exact
job-id capture, the placeholder substitution, the self-contained batch (snapshot order,
`snapshot_dirs`, the binary copy, repointed configs, the live-tree refusal of every
path spelling, skipped and symlinked snapshot dirs with their shell run in a local
`sh`, which `rollup.py` rollup/combine run, combine's normalized exps through the real
`rollup.py` merge and its `--out-name` guard), the submit lock and batch-id claim, and
the status/rollup lifecycle (cluster faked — no network).
