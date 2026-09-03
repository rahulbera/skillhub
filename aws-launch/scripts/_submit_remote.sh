#!/bin/bash
# Remote batch submitter — runs on the head node as the remote user (via the
# backend's `submit`). Knobs arrive in a FILE (never inline) so there is no SSM
# quoting to get wrong.
#   args: RESULTS_BUCKET TRACES_FILE EXPS_FILE PARTITION NCORES [REGION]
#   EXPS_FILE lines: "<exp><TAB><knobs...>"
#   prints one "SUBMIT <exp> <trace> <jobid>" per queued job.
set -uo pipefail
export PATH=/opt/slurm/bin:/usr/local/bin:/usr/bin:/bin
RESULTS_BUCKET="$1"; TRACES_FILE="$2"; EXPS_FILE="$3"; PARTITION="$4"; NCORES="$5"; REGION="${6:-us-east-1}"
cd "$HOME"
aws s3 cp "$RESULTS_BUCKET/bootstrap/champsim-job.sh" ./champsim-job.sh --region "$REGION" --no-progress >/dev/null
chmod +x champsim-job.sh
mkdir -p results
while IFS=$'\t' read -r EXP KNOBS || [ -n "${EXP:-}" ]; do
  [ -z "${EXP:-}" ] && continue
  while IFS= read -r T || [ -n "${T:-}" ]; do
    [ -z "${T:-}" ] && continue
    if JID=$(sbatch --parsable --time=00:40:00 -p "$PARTITION" -c "$NCORES" \
              -J "${EXP}-${T%.champsim2.zst}" champsim-job.sh "$T" "$EXP" "$KNOBS" 2>sb.err); then
      echo "SUBMIT $EXP $T $JID"
    else
      echo "SBATCH_FAIL $EXP $T: $(tail -1 sb.err)" >&2
    fi
  done < "$TRACES_FILE"
done < "$EXPS_FILE"
