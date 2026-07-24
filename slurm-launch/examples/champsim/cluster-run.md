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

The orchestrator runs `create_jobfile.py` / `rollup.py` **on the cluster** (champsim-infra
is rsynced up too). Jobs use `--no-trace-cache` (cluster NFS handles concurrent reads).
All script changes are additive/behind flags, so `regression/run_regression.py` is unaffected.

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

The local source files are never mutated; `submit` rsyncs the sim + champsim-infra,
stages the tlist/exp/mfile into `<run_dir>/inputs/`, builds, smoke-tests pair 0 on the
login node, and only on pass submits every job. A failed smoke/build launches nothing.

### `$(SIM_HOME_IN_CLUSTER)` placeholder

Trace paths in the tlist are already cluster-absolute, but exp files often reference
files **inside** the sim tree (e.g. `--config=$(SIM_HOME_IN_CLUSTER)/config/x.ini`),
whose cluster-absolute path isn't known until rsync. Write that literal placeholder; at
submit time the orchestrator resolves it to `remote_sim_path` on temp copies before
staging (your source files stay untouched). Other `$(...)` tokens are left for
`create_jobfile`/`rollup` to resolve from their own `definitions`.

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
   foreground timeout can't kill it mid-build (which would leave no ledger).
6. **Partial submits aren't orphaned:** if some `sbatch` succeed and some fail, the
   launched jobs are still recorded (`status: partial`) so you can track/cancel them.

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
job-id capture, the placeholder substitution, and the status/rollup lifecycle (cluster
faked — no network).
