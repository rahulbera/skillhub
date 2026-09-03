# ChampSim/Hermes backend notes (the reference workload)

The bundled `scripts/aws_launch.py` is the reference AWS backend, wired for
Hermes (github.com/CMU-SAFARI/Hermes). Concrete choices for this workload:

## Build
`build_command: ./build_champsim.sh glc multi multi multi multi 1 1 0` → produces
`bin/glc-perceptron-no-multi-multi-multi-multi-1core-1ch` (a "multi" build; the
prefetcher/replacement is chosen **per run** via knobs). `libbf` is a gitignored
vendored dep (clone github.com/mavam/libbf into `libbf/`, cmake+make once).
Ubuntu deps: `build-essential cmake git perl xz-utils gzip zlib1g-dev liblzma-dev libzstd-dev`.

## The v2-trace gotcha (do not skip)
The SPEC26 traces are v2 format (`.champsim2.zst`) but the `trace_version` knob
**defaults to 1** — reading a v2 trace as v1 crashes (`va_to_pa Assertion 0`) or
parses wrong. **Every run needs `--trace_version=2`** — it is baked into
`default_knobs` and the smoke job. Keep it in any custom knob string.

## Experiments (the `--exps` argument to submit)
Semicolon-separated `name=knobs`. Empty knobs → `default_knobs` (nopref baseline).
Reference pair (from Hermes `experiments/MICRO22_AE.exp`):
```
--exps "nopref=;pythia=<default_knobs> --l2c_prefetcher_types=scooby --config={REMOTE_REPO}/config/pythia.ini --scooby_enable_direct_pref_issue=true --scooby_pref_at_lower_level=true --scooby_dyn_degrees_type2=1,1,2,4"
```
(For Pythia, {REMOTE_REPO} expands to remote_repo_path.)

## Traces (the `--traces` argument)
A file with one trace filename per line (basenames under
`s3://champsim-traces-all/version2/spec26/`), e.g. the 7xx SPECRate set.

## Result shape
Each job writes champsim stdout to
`s3://champsim-results-all/results/<trace_stem>-<exp>-j<jobid>.txt`; `collect`
parses `Finished CPU 0 ... cumulative IPC:` into `summary.csv`.

## Validated
This flow was validated end-to-end on the `champsim` cluster: wake → build →
smoke → 120-job array (2 configs × 60 traces) auto-scaling the Spot fleet 0→2→0,
results to S3, IPC collected and cross-checked against an x86 baseline
(nopref bit-identical; Pythia speedup aligned within ~2%).
