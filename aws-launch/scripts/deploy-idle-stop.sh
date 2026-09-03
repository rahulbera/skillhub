#!/bin/bash
# Runs on head node as root (via SSM). Updates idle auto-stop: 15 min + active-session guard.
set -uo pipefail
IDLE_MINUTES=15

cat > /usr/local/bin/champsim-idle-stop.sh <<'SCRIPT'
#!/bin/bash
# Stop the head node after the Slurm queue has been empty AND no interactive
# session for IDLE_MINUTES. Fail-safe: any uncertainty (squeue error) -> do NOT stop.
export PATH=/opt/slurm/bin:/usr/local/bin:/usr/bin:/bin
IDLE_MINUTES=__IDLE__
STATE=/var/lib/champsim-idle-since
OUT=$(squeue -h -t pending,running,configuring,completing 2>/dev/null); rc=$?
if [ $rc -ne 0 ]; then logger -t champsim-idle-stop "squeue rc=$rc; skip (fail-safe)"; exit 0; fi
NJOBS=$(printf '%s' "$OUT" | grep -c .)
# active-session guard: logged-in users OR interactive SSM (Session Manager) shells
LOGIN=$(who 2>/dev/null | grep -c .)
SSM=$(pgrep -fc 'ssm-session-worker' 2>/dev/null); SSM=${SSM:-0}
if [ "$NJOBS" -gt 0 ] || [ "${LOGIN:-0}" -gt 0 ] || [ "$SSM" -gt 0 ]; then
  rm -f "$STATE"; logger -t champsim-idle-stop "busy (jobs=$NJOBS login=$LOGIN ssm=$SSM); idle timer reset"; exit 0
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
sed -i "s/__IDLE__/$IDLE_MINUTES/" /usr/local/bin/champsim-idle-stop.sh
chmod +x /usr/local/bin/champsim-idle-stop.sh
systemctl daemon-reload
systemctl restart champsim-idle-stop.timer
echo "=== updated: IDLE_MINUTES=$IDLE_MINUTES + active-session guard ==="
systemctl is-active champsim-idle-stop.timer
echo "--- test run now (this SSM run-command is NOT an interactive session, so guard should see ssm=0) ---"
/usr/local/bin/champsim-idle-stop.sh
echo "idle-since: $(cat /var/lib/champsim-idle-since 2>/dev/null || echo '(reset/busy)')"
echo "who: $(who | wc -l) logins; ssm-session-worker: $(pgrep -fc ssm-session-worker 2>/dev/null || echo 0)"
