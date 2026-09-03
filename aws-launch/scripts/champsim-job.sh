#!/bin/bash
#SBATCH -p sim-spot
#SBATCH -c 1
#SBATCH -o /home/ubuntu/results/slurm-%x-%j.out
#SBATCH -e /home/ubuntu/results/slurm-%x-%j.err
#
# ChampSim job for the AWS Spot Slurm cluster (trace-local model).
#   prolog:  stage one trace  S3 -> node-local NVMe /scratch
#   run:     champsim <knobs> -traces /scratch/<trace>
#   epilog:  push result -> S3, clean scratch
# Usage:  sbatch champsim-job.sh <trace_filename> [exp_label] ["<knobs>"]
set -uo pipefail

TRACE_NAME="${1:?trace filename required, e.g. 708.sqlite_r-cte_size2000-233B.champsim2.zst}"
EXP="${2:-nopref}"
KNOBS="${3:---warmup_instructions=1000000 --simulation_instructions=1000000 --trace_version=2 --llc_replacement_type=ship --config=/home/ubuntu/Hermes/config/nopref.ini --num_rob_partitions=3 --rob_partition_size=64,128,320 --rob_frontal_partition_ids=0 --rob_dorsal_partition_ids=2}"

EXE=/home/ubuntu/Hermes/bin/glc-perceptron-no-multi-multi-multi-multi-1core-1ch
S3_TRACES=s3://champsim-traces-all/version2/spec26
S3_RESULTS=s3://champsim-results-all/results
SCRATCH=/scratch
RESDIR=/home/ubuntu/results
mkdir -p "$RESDIR"

STAMP="${TRACE_NAME%.champsim2.zst}-${EXP}-j${SLURM_JOB_ID}"
LOCAL_TRACE="$SCRATCH/${SLURM_JOB_ID}-${TRACE_NAME}"
OUT="$RESDIR/${STAMP}.txt"

echo "[node] $(hostname)  job=$SLURM_JOB_ID  trace=$TRACE_NAME  exp=$EXP"
echo "[scratch] $(findmnt -no SOURCE,FSTYPE,SIZE --target $SCRATCH 2>/dev/null || echo 'not a mount') | df: $(df -h $SCRATCH | tail -1)"

# --- prolog: stage trace to node-local NVMe ---
t0=$(date +%s)
aws s3 cp "$S3_TRACES/$TRACE_NAME" "$LOCAL_TRACE" --region us-east-1 --no-progress
echo "[stage-in] $(du -h "$LOCAL_TRACE" | cut -f1) in $(( $(date +%s)-t0 ))s"

# --- run ---
echo "[run] $EXE $KNOBS -traces $LOCAL_TRACE"
t1=$(date +%s)
$EXE $KNOBS -traces "$LOCAL_TRACE" > "$OUT" 2>&1
rc=$?
echo "[run] champsim rc=$rc in $(( $(date +%s)-t1 ))s"

# --- epilog: results -> S3, cleanup scratch ---
aws s3 cp "$OUT" "$S3_RESULTS/${STAMP}.txt" --region us-east-1 --no-progress
rm -f "$LOCAL_TRACE"
echo "[done] s3: $S3_RESULTS/${STAMP}.txt  rc=$rc"
exit $rc
