#!/bin/bash
# Remote batch submitter — runs on the head node as the remote user (via the
# backend's `submit`). Knobs arrive in a FILE (never inline) so there is no SSM
# quoting to get wrong.
#
#   args: BOOT_PREFIX PROJECT_ROOT TRACES_FILE EXPS_FILE PARTITION NCORES WALLTIME REGION BATCH ASSETS TOKEN
#     BOOT_PREFIX   s3://<results-bucket>/bootstrap/<project>   (per-PROJECT, never shared)
#     PROJECT_ROOT  /home/<user>/<project>  — holds Hermes/, results/, run-assets/
#     BATCH         the record's S3 name, <batch>.<attempt>: per attempt, so no other attempt's record
#                   can pass for this one's
#     ASSETS        the batch whose snapshot, run-assets/<ASSETS>/, the jobs run from; its
#                   rendered wrapper is $BOOT/<ASSETS>.champsim-job.sh
#     TOKEN         the attempt token; the record's first line is "ATTEMPT <TOKEN>"
#   EXPS_FILE lines: "<exp><TAB><knobs...>"
#   writes one "SUBMIT <exp> <trace> <jobid>" per queued job to submitted.txt, closes it with
#   "SUBMIT_COMPLETE <n>", and uploads it to $BOOT/$BATCH.submitted.txt. The backend accepts
#   it only with this attempt's token and that closing line. The SUBMIT lines are ALSO echoed
#   to stdout, but stdout is NOT the source of truth: SSM truncates StandardOutputContent at
#   24,000 chars, which silently drops the tail at roughly 340 jobs. The S3 file is authoritative.
#
# Everything happens inside $PROJECT_ROOT/run-assets/<ASSETS>, never $HOME: two batches,
# projects or users submitting at once must not overwrite each other's wrapper or scratch files.
set -uo pipefail
export PATH=/opt/slurm/bin:/usr/local/bin:/usr/bin:/bin
BOOT="$1"; PROJECT_ROOT="$2"; TRACES_FILE="$3"; EXPS_FILE="$4"
PARTITION="$5"; NCORES="$6"; WALLTIME="${7:-24:00:00}"; REGION="${8:-us-east-1}"
BATCH="${9:-batch}"; ASSETS="${10:?the batch whose run-assets snapshot the jobs use}"
TOKEN="${11:?the attempt token the record must carry}"

RUNDIR="$PROJECT_ROOT/run-assets/$ASSETS"
mkdir -p "$RUNDIR" "$PROJECT_ROOT/results"
cd "$RUNDIR" || { echo "cannot cd $RUNDIR" >&2; exit 1; }

# Pull this batch's own wrapper. Rendered per batch by the backend, so the #SBATCH log
# paths point at THIS project and the binary is THIS batch's snapshot.
aws s3 cp "$BOOT/$ASSETS.champsim-job.sh" ./champsim-job.sh --region "$REGION" --no-progress >/dev/null \
  || { echo "cannot fetch $BOOT/$ASSETS.champsim-job.sh" >&2; exit 1; }
chmod +x champsim-job.sh

printf 'ATTEMPT %s\n' "$TOKEN" > submitted.txt
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
echo "SUBMIT_COMPLETE $NSUB" >> submitted.txt   # a record without it was cut short

# The authoritative record: stdout may be truncated by SSM, this file is not. It holds the queued
# job ids, so one failed upload is retried, and a lasting failure names the head-node copy.
for i in 1 2 3; do
  aws s3 cp submitted.txt "$BOOT/$BATCH.submitted.txt" --region "$REGION" --no-progress >/dev/null && break
  if [ "$i" = 3 ]; then echo "SUBMIT_RECORD_UPLOAD_FAILED $RUNDIR/submitted.txt"; exit 1; fi
  sleep $((i * 2))
done
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
