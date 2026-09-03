#!/bin/bash
# aws-champsim-wake.sh — ensure the champsim head node is up and Slurm is ready.
# Idempotent: safe to call whether the head is stopped, stopping, or already running.
# Needs the champsim profile (ec2:Describe/Start/Stop + ssm). Prints the head instance id last.
set -uo pipefail
export AWS_PROFILE=${AWS_PROFILE:-champsim}
REGION=us-east-1
CLUSTER=champsim

HEAD=$(aws ec2 describe-instances --region "$REGION" \
  --filters "Name=tag:parallelcluster:cluster-name,Values=$CLUSTER" \
            "Name=tag:parallelcluster:node-type,Values=HeadNode" \
            "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query 'Reservations[0].Instances[0].InstanceId' --output text 2>/dev/null)
if [ -z "$HEAD" ] || [ "$HEAD" = "None" ]; then echo "[wake] ERROR: head node not found" >&2; exit 1; fi
state=$(aws ec2 describe-instances --instance-ids "$HEAD" --region "$REGION" \
  --query 'Reservations[0].Instances[0].State.Name' --output text)
echo "[wake] head=$HEAD state=$state" >&2

case "$state" in
  stopped)  echo "[wake] starting..." >&2; aws ec2 start-instances --instance-ids "$HEAD" --region "$REGION" >/dev/null
            aws ec2 wait instance-running --instance-ids "$HEAD" --region "$REGION"; echo "[wake] running" >&2;;
  stopping) echo "[wake] waiting for stop then start..." >&2; aws ec2 wait instance-stopped --instance-ids "$HEAD" --region "$REGION"
            aws ec2 start-instances --instance-ids "$HEAD" --region "$REGION" >/dev/null
            aws ec2 wait instance-running --instance-ids "$HEAD" --region "$REGION";;
  running)  echo "[wake] already running" >&2;;
  *)        aws ec2 wait instance-running --instance-ids "$HEAD" --region "$REGION";;
esac

# SSM agent online
for i in $(seq 1 40); do
  ping=$(aws ssm describe-instance-information --region "$REGION" \
    --filters "Key=InstanceIds,Values=$HEAD" --query 'InstanceInformationList[0].PingStatus' --output text 2>/dev/null)
  [ "$ping" = "Online" ] && { echo "[wake] SSM online" >&2; break; }
  sleep 5
done

# slurmctld responsive
for i in $(seq 1 40); do
  CID=$(aws ssm send-command --instance-ids "$HEAD" --region "$REGION" --document-name AWS-RunShellScript \
    --parameters 'commands=["export PATH=/opt/slurm/bin:$PATH; sinfo -h >/dev/null 2>&1 && echo SLURM_OK || echo SLURM_WAIT"]' \
    --query 'Command.CommandId' --output text 2>/dev/null)
  sleep 5
  out=$(aws ssm get-command-invocation --command-id "$CID" --instance-id "$HEAD" --region "$REGION" \
    --query StandardOutputContent --output text 2>/dev/null)
  echo "$out" | grep -q SLURM_OK && { echo "[wake] slurmctld ready" >&2; echo "$HEAD"; exit 0; }
  sleep 4
done
echo "[wake] WARNING: slurmctld not confirmed ready" >&2
echo "$HEAD"
