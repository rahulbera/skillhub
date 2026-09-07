#!/bin/bash
# Remote batch submitter — runs on the head node as the remote user (via the
# backend's `submit`). Knobs arrive in a FILE (never inline) so there is no SSM
# quoting to get wrong.
#
#   args: BOOT_PREFIX PROJECT_ROOT TRACES_FILE EXPS_FILE PARTITION NCORES WALLTIME [REGION]
#     BOOT_PREFIX   s3://<results-bucket>/bootstrap/<project>   (per-PROJECT, never shared)
#     PROJECT_ROOT  /home/<user>/<project>  — holds Hermes/, results/, run-assets/
#   EXPS_FILE lines: "<exp><TAB><knobs...>"
#   prints one "SUBMIT <exp> <trace> <jobid>" per queued job.
#
# Everything happens inside $PROJECT_ROOT/run-assets, never $HOME: two projects or two
# users submitting at once must not overwrite each other's wrapper or scratch files.
set -uo pipefail
export PATH=/opt/slurm/bin:/usr/local/bin:/usr/bin:/bin
BOOT="$1"; PROJECT_ROOT="$2"; TRACES_FILE="$3"; EXPS_FILE="$4"
PARTITION="$5"; NCORES="$6"; WALLTIME="${7:-24:00:00}"; REGION="${8:-us-east-1}"

RUNDIR="$PROJECT_ROOT/run-assets"
mkdir -p "$RUNDIR" "$PROJECT_ROOT/results"
cd "$RUNDIR" || { echo "cannot cd $RUNDIR" >&2; exit 1; }

# Pull this project's own wrapper. Rendered per project by the backend, so the
# #SBATCH log paths and PROJECT_ROOT default already point at THIS project.
aws s3 cp "$BOOT/champsim-job.sh" ./champsim-job.sh --region "$REGION" --no-progress >/dev/null \
  || { echo "cannot fetch $BOOT/champsim-job.sh" >&2; exit 1; }
chmod +x champsim-job.sh

export PROJECT_ROOT
while IFS=$'\t' read -r EXP KNOBS || [ -n "${EXP:-}" ]; do
  [ -z "${EXP:-}" ] && continue
  while IFS= read -r T || [ -n "${T:-}" ]; do
    [ -z "${T:-}" ] && continue
    STEM=$(basename "$T"); STEM="${STEM%.gz}"; STEM="${STEM%.zst}"; STEM="${STEM%.xz}"
    STEM="${STEM%.champsim2}"; STEM="${STEM%.champsim}"; STEM="${STEM%.trace}"
    if JID=$(sbatch --parsable --requeue --time="$WALLTIME" -p "$PARTITION" -c "$NCORES" \
              --export=ALL,PROJECT_ROOT="$PROJECT_ROOT" \
              -J "${EXP}-${STEM}" champsim-job.sh "$T" "$EXP" "$KNOBS" 2>sb.err); then
      echo "SUBMIT $EXP $T $JID"
    else
      echo "SBATCH_FAIL $EXP $T: $(tail -1 sb.err)" >&2
    fi
  done < "$TRACES_FILE"
done < "$EXPS_FILE"
