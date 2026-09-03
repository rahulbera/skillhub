#!/bin/bash
# Runs on the head node as root (via SSM). Installs/updates the head-node idle
# auto-stop: IDLE_MINUTES with a full activity guard.
#
# Guards (any one keeps the node alive):
#   - Slurm queue non-empty (pending/running/configuring/completing)
#   - a logged-in user, or an interactive SSM Session Manager shell
#   - a running SSM send-command (ssm-document-worker)  <-- automation
#   - a workload process matching WORKLOAD_PATTERN on the head node
#   - the explicit lock /var/lib/champsim-busy (touch it; ignored once stale)
#
# Usage: deploy-idle-stop.sh [IDLE_MINUTES] [WORKLOAD_PATTERN]
set -uo pipefail
IDLE_MINUTES="${1:-30}"
WORKLOAD_PATTERN="${2:-/Hermes/bin/}"
BUSY_MAX_MIN=45

cat > /usr/local/bin/champsim-idle-stop.sh <<'SCRIPT'
#!/bin/bash
# Stop the head node once the Slurm queue has been empty AND nothing else is
# active for IDLE_MINUTES. Fail-safe: any uncertainty (squeue error) -> do NOT stop.
export PATH=/opt/slurm/bin:/usr/local/bin:/usr/bin:/bin
IDLE_MINUTES=__IDLE__
BUSY_MAX_MIN=__BUSYMAX__
WORKLOAD_PATTERN='__WLPAT__'
STATE=/var/lib/champsim-idle-since
BUSY=/var/lib/champsim-busy
OUT=$(squeue -h -t pending,running,configuring,completing 2>/dev/null); rc=$?
if [ $rc -ne 0 ]; then logger -t champsim-idle-stop "squeue rc=$rc; skip (fail-safe)"; exit 0; fi
NJOBS=$(printf '%s' "$OUT" | grep -c .)
# interactive guard: logged-in users OR SSM Session Manager shells
LOGIN=$(who 2>/dev/null | grep -c .)
SSM=$(pgrep -fc 'ssm-session-worker' 2>/dev/null); SSM=${SSM:-0}
# automation guard: a running SSM send-command counts as activity. Without this a
# long `aws ssm send-command` run gets shut down underneath itself.
DOC=$(pgrep -fc 'ssm-document-worker' 2>/dev/null); DOC=${DOC:-0}
# workload guard: a sim running on the head node itself. Keep this pattern tight --
# a loose one (e.g. 'champsim') self-matches this script's own path and would pin
# the node up forever.
SIM=$(pgrep -fc "$WORKLOAD_PATTERN" 2>/dev/null); SIM=${SIM:-0}
# explicit lock: `sudo touch /var/lib/champsim-busy` to hold the node up. Ignored
# once older than BUSY_MAX_MIN so a crashed job cannot pin it indefinitely.
LOCK=0
if [ -f "$BUSY" ]; then
  age_min=$(( ( $(date +%s) - $(stat -c %Y "$BUSY") ) / 60 ))
  if [ "$age_min" -lt "$BUSY_MAX_MIN" ]; then LOCK=1; else rm -f "$BUSY"; fi
fi
if [ "$NJOBS" -gt 0 ] || [ "${LOGIN:-0}" -gt 0 ] || [ "$SSM" -gt 0 ] \
   || [ "$DOC" -gt 0 ] || [ "$SIM" -gt 0 ] || [ "$LOCK" -gt 0 ]; then
  rm -f "$STATE"
  logger -t champsim-idle-stop "busy (jobs=$NJOBS login=$LOGIN ssm=$SSM doc=$DOC sim=$SIM lock=$LOCK); idle timer reset"
  exit 0
fi
now=$(date +%s)
[ -f "$STATE" ] || { echo "$now" > "$STATE"; logger -t champsim-idle-stop "idle (empty queue, no session); timer started"; exit 0; }
since=$(cat "$STATE" 2>/dev/null || echo "$now")
idle_min=$(( (now - since) / 60 ))
logger -t champsim-idle-stop "idle ${idle_min}/${IDLE_MINUTES} min"
if [ "$idle_min" -ge "$IDLE_MINUTES" ]; then
  TOKEN=$(curl -sX PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 120")
  IID=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)
  logger -t champsim-idle-stop "idle threshold reached; stopping self ($IID)"
  aws ec2 stop-instances --instance-ids "$IID" --region us-east-1 >>/var/log/champsim-idle-stop.log 2>&1
fi
SCRIPT
sed -i "s/__IDLE__/$IDLE_MINUTES/; s/__BUSYMAX__/$BUSY_MAX_MIN/; s|__WLPAT__|$WORKLOAD_PATTERN|" \
    /usr/local/bin/champsim-idle-stop.sh
chmod +x /usr/local/bin/champsim-idle-stop.sh
systemctl daemon-reload
systemctl restart champsim-idle-stop.timer
echo "=== updated: IDLE_MINUTES=$IDLE_MINUTES  workload='$WORKLOAD_PATTERN' ==="
systemctl is-active champsim-idle-stop.timer
echo "--- test run (an SSM send-command now counts as busy: expect doc>=1) ---"
/usr/local/bin/champsim-idle-stop.sh
journalctl -t champsim-idle-stop -n 1 --no-pager
