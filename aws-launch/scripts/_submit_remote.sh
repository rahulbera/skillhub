#!/bin/bash
# Remote batch submitter — runs on the head node as the remote user (via the
# backend's `submit`). Knobs arrive in a FILE (never inline) so there is no SSM
# quoting to get wrong.
#
#   args: BOOT_PREFIX PROJECT_ROOT TRACES_FILE EXPS_FILE PARTITION NCORES WALLTIME [REGION] [BATCH]
#     BOOT_PREFIX   s3://<results-bucket>/bootstrap/<project>   (per-PROJECT, never shared)
#     PROJECT_ROOT  /home/<user>/<project>  — holds Hermes/, results/, run-assets/
#   EXPS_FILE lines: "<exp><TAB><knobs...>"
#   writes one "SUBMIT <exp> <trace> <jobid>" per queued job to submitted.txt and
#   uploads it to $BOOT/$BATCH.submitted.txt. It is ALSO echoed to stdout, but stdout
#   is NOT the source of truth: SSM truncates StandardOutputContent at 24,000 chars,
#   which silently drops the tail at roughly 340 jobs. The S3 file is authoritative.
#
# Everything happens inside $PROJECT_ROOT/run-assets, never $HOME: two projects or two
# users submitting at once must not overwrite each other's wrapper or scratch files.
set -uo pipefail
export PATH=/opt/slurm/bin:/usr/local/bin:/usr/bin:/bin
BOOT="$1"; PROJECT_ROOT="$2"; TRACES_FILE="$3"; EXPS_FILE="$4"
PARTITION="$5"; NCORES="$6"; WALLTIME="${7:-24:00:00}"; REGION="${8:-us-east-1}"
BATCH="${9:-batch}"

RUNDIR="$PROJECT_ROOT/run-assets"
mkdir -p "$RUNDIR" "$PROJECT_ROOT/results"
cd "$RUNDIR" || { echo "cannot cd $RUNDIR" >&2; exit 1; }

# Pull this project's own wrapper. Rendered per project by the backend, so the
# #SBATCH log paths and PROJECT_ROOT default already point at THIS project.
aws s3 cp "$BOOT/champsim-job.sh" ./champsim-job.sh --region "$REGION" --no-progress >/dev/null \
  || { echo "cannot fetch $BOOT/champsim-job.sh" >&2; exit 1; }
chmod +x champsim-job.sh

: > submitted.txt
: > failed.txt
NSUB=0; NFAIL=0
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
      echo "SUBMIT $EXP $T $JID" | tee -a submitted.txt
      NSUB=$((NSUB+1))
    else
      printf '%s\t%s\t%s\n' "$EXP" "$T" "$(tail -1 sb.err)" >> failed.txt
      NFAIL=$((NFAIL+1))
    fi
  done < "$TRACES_FILE"
done < "$EXPS_FILE"

# The authoritative record: stdout may be truncated by SSM, this file is not.
aws s3 cp submitted.txt "$BOOT/$BATCH.submitted.txt" --region "$REGION" --no-progress >/dev/null \
  || { echo "cannot upload submitted.txt to $BOOT" >&2; exit 1; }
if [ "$NFAIL" -gt 0 ]; then
  aws s3 cp failed.txt "$BOOT/$BATCH.failed.txt" --region "$REGION" --no-progress >/dev/null || true
  # Summarise by DISTINCT reason. When a whole batch is rejected it is almost always
  # one cause (an inactive partition, a bad -p, a quota); printing it once keeps the
  # diagnosis readable and inside SSM's output cap.
  echo "SUBMIT_FAILED $NFAIL"
  cut -f3 failed.txt | sort | uniq -c | sort -rn | head -5 | while read -r n reason; do
    echo "SUBMIT_FAIL_REASON ${n}x ${reason}"
  done
fi
echo "SUBMIT_COMPLETE $NSUB"
