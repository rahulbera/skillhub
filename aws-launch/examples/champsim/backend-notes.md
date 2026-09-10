# ChampSim/Hermes backend notes (the reference workload)

The bundled `scripts/aws_launch.py` is the reference AWS backend, wired for
Hermes (github.com/CMU-SAFARI/Hermes). Concrete choices for this workload:

## Build
`build_command: ./build_champsim.sh glc multi multi multi multi 1 1 0` → produces
`bin/glc-perceptron-no-multi-multi-multi-multi-1core-1ch` (a "multi" build; the
prefetcher/replacement is chosen **per run** via knobs). `libbf` is a gitignored
vendored dep (clone github.com/mavam/libbf into `libbf/`, cmake+make once).
Ubuntu deps: `build-essential cmake git perl xz-utils gzip zlib1g-dev liblzma-dev libzstd-dev`.

`submit` (or `sync --build`) first mirrors the local tree onto the head node, then
rebuilds unconditionally. `libbf`, `bin` and `obj` are `protect`ed in `sync:`, so libbf
is built on the head node once and the mirror neither ships nor deletes it.
Each batch runs a real copy of the binary, so the next build cannot reach a queued batch
whether it renames the new binary into place (as `build_champsim.sh` does) or writes over it.

## The v2-trace gotcha (do not skip)
The SPEC26 traces are v2 format (`.champsim2.zst`) but the `trace_version` knob
**defaults to 1** — reading a v2 trace as v1 crashes (`va_to_pa Assertion 0`) or
parses wrong. **Every run needs `--trace_version=2`** — it is baked into
`default_knobs` and the smoke job. Keep it in any custom knob string.

## The jobfile route (`--tlist`/`--exp`/`--mfile`)
The same sweep as champsim-infra YAML, launched by its `create_jobfile.py`:
```
submit --tlist spec26.tlist.yml --exp arms.exp.yml --mfile ipc.mfile.yml --label arms-1
```
- **tlist** `path:` is what each job's `fetch_trace.py` reads — on this cluster
  `s3://champsim-traces-all/version2/spec26/<trace>.champsim2.zst` — and `version: 2`
  becomes `--trace-version=2`, so leave the version out of the exp.
- **exp** config paths go through `{REMOTE_REPO}/config/<x>.ini`, inline or through
  definitions (`- CFG: "{REMOTE_REPO}/config"`, then `--config=$(CFG)/pythia.ini`). `submit`
  expands each file's `$(VAR)`s as create_jobfile does, points every path into `config/` at the
  batch's own copy in `<exp>.resolved.yml` (no definitions left), and refuses any other path
  into the live Hermes or champsim-infra tree in the spellings SKILL.md lists. Keep each exp file
  self-contained (create_jobfile resolves definitions per file, in one pass: a definition that
  uses another is `unresolved_variable`).
- **mfile** lists the stats `collect` rolls up, e.g. `- ipc: "$(Core_0_cumulative_IPC)"`. Keep
  an `ipc` metric: `collect` flags rows where it is 0.
- The head node needs PyYAML for `python3`, and `sync:` must mirror `champsim-infra`.

## Experiments (the wrapper route's `--exps` argument)
Semicolon-separated `name=knobs`. Empty knobs → `default_knobs` (nopref baseline). A name uses
letters, digits and `._+-` (starting with a letter or digit), and a trace key holds no whitespace:
`submit` refuses anything else before waking (`bad_input_name`, `bad_input`), since the job-id record
is one `SUBMIT <exp> <trace> <jobid>` line per job.
Reference pair (from Hermes `experiments/MICRO22_AE.exp`):
```
--exps "nopref=;pythia=<default_knobs> --l2c_prefetcher_types=scooby --config={REMOTE_REPO}/config/pythia.ini --scooby_enable_direct_pref_issue=true --scooby_pref_at_lower_level=true --scooby_dyn_degrees_type2=1,1,2,4"
```
(For Pythia, {REMOTE_REPO}/config/ becomes the batch's config snapshot under run-assets/<batch>/.)

## Traces (the `--traces` argument)
A file with one trace filename per line (basenames under
`s3://champsim-traces-all/version2/spec26/`), e.g. the 7xx SPECRate set.

## Result shape
Wrapper route: each job writes champsim stdout to
`s3://champsim-results-all/results/<trace_stem>-<exp>-j<jobid>.txt`; `collect`
parses `Finished CPU 0 ... cumulative IPC:` into `summary.csv`.
Jobfile route: each job writes `<trace>_<exp>.out/.err` into
`<remote_project_root>/results/<batch>/` on the head node; `collect` rolls them up there into
`stats.csv` (`TraceName, ExpName, <metrics…>, Filter`) and copies back only that and
`rollup_report.json`.

## Validated
This flow was validated end-to-end on the `champsim` cluster: wake → build →
smoke → 120-job array (2 configs × 60 traces) auto-scaling the Spot fleet 0→2→0,
results to S3, IPC collected and cross-checked against an x86 baseline
(nopref bit-identical; Pythia speedup aligned within ~2%).
