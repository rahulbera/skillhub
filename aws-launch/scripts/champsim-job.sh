#!/bin/bash
#SBATCH -p sim-spot
#SBATCH -c 1
#SBATCH -o {{PROJECT_ROOT}}/results/slurm-%x-%j.out
#SBATCH -e {{PROJECT_ROOT}}/results/slurm-%x-%j.err
#
# ChampSim job for the AWS Spot Slurm cluster (trace-local model).
#   prolog:  stage one trace  S3 -> node-local NVMe /scratch
#   run:     champsim <knobs> -traces /scratch/<trace>
#   epilog:  push result -> S3, clean scratch
# Usage:  sbatch champsim-job.sh <trace_key> [exp_label] ["<knobs>"] [s3_result_prefix]
#   <trace_key> is relative to the TRACES BUCKET ROOT and MAY contain subdirectories,
#   e.g. "version2/dlrm/dlrm_..._s0.champsim2.zst" (v2) or
#        "google/merced/merced_0020.champsim.gz" (v1).
#   Pass the matching --trace_version in <knobs>: v2 traces REQUIRE --trace_version=2.
set -uo pipefail

TRACE_KEY="${1:?trace key required, e.g. version2/dlrm/dlrm_..._s0.champsim2.zst}"
EXP="${2:-nopref}"
KNOBS="${3:---warmup_instructions=1000000 --simulation_instructions=1000000 --trace_version=2 --llc_replacement_type=ship --config={{PROJECT_ROOT}}/Hermes/config/nopref.ini --num_rob_partitions=3 --rob_partition_size=64,128,320 --rob_frontal_partition_ids=0 --rob_dorsal_partition_ids=2}"
# S3 prefix under the results bucket. Owner-first layout: <owner>/<project>/results/<campaign>
RES_PREFIX="${4:-{{PROJECT}}/results}"

# PROJECT_ROOT selects which project's ChampSim + results tree to use, so several
# projects (each with its own ChampSim build) can share this one wrapper. Override by
# exporting PROJECT_ROOT before sbatch, or with `sbatch --export=ALL,PROJECT_ROOT=...`.
# NOTE: the #SBATCH -o/-e lines above CANNOT use a variable -- Slurm parses them before
# any shell runs -- so a project with a different root must edit those two lines too.
PROJECT_ROOT="${PROJECT_ROOT:-{{PROJECT_ROOT}}}"
EXE="$PROJECT_ROOT/Hermes/bin/glc-perceptron-no-multi-multi-multi-multi-1core-1ch"
S3_TRACES=s3://champsim-traces-all
S3_RESULTS="s3://champsim-results-all/${RES_PREFIX}"
SCRATCH=/scratch
RESDIR="$PROJECT_ROOT/results"
mkdir -p "$RESDIR"

TRACE_NAME=$(basename "$TRACE_KEY")
# strip the compression suffix then the champsim marker, so both
# "<x>.champsim2.zst" (v2) and "<x>.champsim.gz" (v1) reduce to "<x>"
STEM="${TRACE_NAME%.gz}"; STEM="${STEM%.zst}"; STEM="${STEM%.xz}"
STEM="${STEM%.champsim2}"; STEM="${STEM%.champsim}"
STAMP="${STEM}__${EXP}"
LOCAL_TRACE="$SCRATCH/${SLURM_JOB_ID}-${TRACE_NAME}"
# Stats are written to node-local NVMe, NOT to the NFS home. S3 is the system of record;
# keeping a second copy on the head node grew /home by ~14 GB per campaign on a 100 GB
# volume. /scratch dies with the node, so nothing accumulates.
# Trade-off: if the node is SIGKILLed mid-run the partial file is lost with it. That is
# acceptable because (i) the epilog below uploads even on a non-zero exit, so real crashes
# and deadlocks are still captured, and (ii) the SIGTERM trap requeues the job. The Slurm
# .out/.err logs stay on NFS either way, so there is always a record of what happened.
OUT="$SCRATCH/${STAMP}.txt"

echo "[node] $(hostname)  job=$SLURM_JOB_ID  trace=$TRACE_KEY  exp=$EXP"
echo "[scratch] $(findmnt -no SOURCE,FSTYPE,SIZE --target $SCRATCH 2>/dev/null || echo 'not a mount') | df: $(df -h $SCRATCH | tail -1)"

# --- prolog: stage trace to node-local NVMe ---
t0=$(date +%s)
if ! aws s3 cp "$S3_TRACES/$TRACE_KEY" "$LOCAL_TRACE" --region us-east-1 --no-progress; then
  echo "[stage-in] FAILED for $TRACE_KEY"; rm -f "$LOCAL_TRACE"; exit 90
fi
echo "[stage-in] $(du -h "$LOCAL_TRACE" | cut -f1) in $(( $(date +%s)-t0 ))s"

# --- run ---
# Defence in depth: Slurm SIGTERMs every job on a node it declares unresponsive
# (and on Spot reclaim / preemption). To Slurm that is a job *completion*, not a
# node failure, so `sbatch --requeue` does NOT cover it -- campaign 2 silently lost
# 121 jobs that way. Requeue ourselves instead of vanishing. Requires champsim to
# run in the background so the trap can fire while we wait.
on_term() {
  echo "[signal] SIGTERM at $(date -u +%FT%TZ) -- requeueing job $SLURM_JOB_ID"
  kill "${CHILD:-0}" 2>/dev/null
  /opt/slurm/bin/scontrol requeue "$SLURM_JOB_ID" 2>/dev/null \
    || scontrol requeue "$SLURM_JOB_ID" 2>/dev/null || true
  rm -f "$LOCAL_TRACE" "$OUT"
  exit 15
}
trap on_term TERM

echo "[run] $EXE $KNOBS -traces $LOCAL_TRACE"
t1=$(date +%s)
$EXE $KNOBS -traces "$LOCAL_TRACE" > "$OUT" 2>&1 &
CHILD=$!
wait "$CHILD"
rc=$?
trap - TERM
echo "[run] champsim rc=$rc in $(( $(date +%s)-t1 ))s"

# --- epilog: results -> S3 (even on failure, to keep deadlock evidence), clean scratch ---
echo "champsim_exit_code $rc" >> "$OUT"
if aws s3 cp "$OUT" "$S3_RESULTS/${EXP}/${STAMP}.txt" --region us-east-1 --no-progress; then
  rm -f "$OUT"
else
  echo "[epilog] UPLOAD FAILED - preserving $OUT on this node's scratch"
fi
rm -f "$LOCAL_TRACE"
echo "[done] s3: $S3_RESULTS/${EXP}/${STAMP}.txt  rc=$rc"
exit $rc
