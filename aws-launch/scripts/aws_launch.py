#!/usr/bin/env python3
"""aws-launch reference backend (ChampSim/Hermes).

Implements the verbs the aws-launch skill drives: configure, submit, status,
collect. Runs LOCALLY on the user's machine; drives the AWS ParallelCluster head
node over SSM and moves inputs/results through S3. All AWS access uses the named
profile from config (never raw keys).

Usage:
  aws_launch.py configure --repo <root> [--profile P --cluster C --force]
  aws_launch.py submit    --repo <root> --traces file --exps name=knobs[,...] [--label L]
  aws_launch.py status    --repo <root> [--batch B]
  aws_launch.py collect   --repo <root> --batch B [--force]

Requires: python3 + PyYAML + AWS CLI v2 locally. See reference/credentials.md.
"""
import argparse, json, os, re, subprocess, sys, tempfile, time
try:
    import yaml
except ImportError:
    sys.exit("ERROR: PyYAML required locally (pip install pyyaml)")

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------- config / ledger ----------
def cfg_path(repo): return os.path.join(repo, ".aws-launch", "config.yml")

def load_cfg(repo):
    p = cfg_path(repo)
    if not os.path.exists(p):
        sys.exit(f"ERROR: no config at {p} — run `configure` (bootstrap) first")
    with open(p) as f:
        cfg = yaml.safe_load(f)
    # Fail loudly on a pre-namespacing config. Without these two keys the backend would
    # fall back to one SHARED bootstrap key and $HOME, so two projects (or two people)
    # would silently overwrite each other's wrapper and results. Refuse instead.
    missing = [k for k in ("project", "remote_project_root") if not cfg.get(k)]
    if missing:
        sys.exit(
            f"ERROR: {p} is missing {missing}.\n"
            "This config predates per-project namespacing. Add, e.g.:\n"
            "  project: my-project\n"
            "  remote_project_root: /home/ubuntu/<user>/<project>\n"
            "and point remote_repo_path at <remote_project_root>/Hermes.\n"
            "See reference/config-template.yml.")
    return cfg

def runs_dir(repo, cfg): return os.path.join(repo, cfg.get("runs_base", ".aws-launch/runs"))

def ledger_path(repo, cfg, batch): return os.path.join(runs_dir(repo, cfg), batch, "ledger.json")


# ---------- aws / ssm helpers ----------
def aws(cfg, args, capture=True, check=True):
    env = dict(os.environ, AWS_PROFILE=cfg["aws_profile"])
    cmd = ["aws"] + args + ["--region", cfg["region"]]
    r = subprocess.run(cmd, env=env, capture_output=capture, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"aws {' '.join(args[:2])} failed: {(r.stderr or r.stdout).strip()}")
    return (r.stdout or "").strip()

def find_head(cfg):
    out = aws(cfg, ["ec2", "describe-instances",
        "--filters", f"Name=tag:parallelcluster:cluster-name,Values={cfg['cluster_name']}",
        "Name=tag:parallelcluster:node-type,Values=HeadNode",
        "Name=instance-state-name,Values=pending,running,stopping,stopped",
        "--query", "Reservations[0].Instances[0].InstanceId", "--output", "text"])
    if not out or out == "None":
        sys.exit(f"ERROR: no head node found for cluster {cfg['cluster_name']}")
    return out

def ssm_run(cfg, head, lines, timeout=600, poll=8):
    """Run shell lines on the head node via SSM run-command; return (status, stdout, stderr)."""
    spec = {"InstanceIds": [head], "DocumentName": "AWS-RunShellScript",
            "Parameters": {"commands": lines}, "TimeoutSeconds": timeout}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
        json.dump(spec, tf); inp = tf.name
    try:
        cid = aws(cfg, ["ssm", "send-command", "--cli-input-json", f"file://{inp}",
                        "--query", "Command.CommandId", "--output", "text"])
        for _ in range(max(1, timeout // poll)):
            time.sleep(poll)
            st = aws(cfg, ["ssm", "get-command-invocation", "--command-id", cid,
                           "--instance-id", head, "--query", "Status", "--output", "text"], check=False)
            if st in ("Success", "Failed", "Cancelled", "TimedOut"):
                out = aws(cfg, ["ssm", "get-command-invocation", "--command-id", cid, "--instance-id", head,
                                "--query", "StandardOutputContent", "--output", "text"], check=False)
                err = aws(cfg, ["ssm", "get-command-invocation", "--command-id", cid, "--instance-id", head,
                                "--query", "StandardErrorContent", "--output", "text"], check=False)
                return st, out, err
        return "TimedOut", "", ""
    finally:
        os.unlink(inp)

def wake(cfg):
    """Idempotent head-node wake via the bundled aws-wake.sh; returns head id."""
    env = dict(os.environ, AWS_PROFILE=cfg["aws_profile"])
    script = os.path.join(SKILL_DIR, "scripts", "aws-wake.sh")
    r = subprocess.run(["bash", script], env=env, capture_output=True, text=True)
    head = (r.stdout or "").strip().splitlines()[-1] if r.stdout.strip() else find_head(cfg)
    if not head.startswith("i-"):
        head = find_head(cfg)
    return head

def s3_put(cfg, local, key):
    aws(cfg, ["s3", "cp", local, key, "--no-progress"])


# ---------- verbs ----------
def verb_configure(args):
    repo = args.repo
    os.makedirs(os.path.join(repo, ".aws-launch"), exist_ok=True)
    p = cfg_path(repo)
    if os.path.exists(p) and not args.force:
        sys.exit(f"config already exists at {p} (use --force to re-bootstrap)")
    tmpl = os.path.join(SKILL_DIR, "reference", "config-template.yml")
    with open(tmpl) as f:
        cfg = yaml.safe_load(f)
    if args.profile: cfg["aws_profile"] = args.profile
    if args.cluster: cfg["cluster_name"] = args.cluster
    # validate profile + head + buckets
    try:
        ident = aws(cfg, ["sts", "get-caller-identity", "--query", "Arn", "--output", "text"])
    except Exception as e:
        print("ERROR validating AWS profile — see reference/credentials.md.\n", e)
        sys.exit("error_id=bad_profile")
    print(f"[configure] identity: {ident}")
    head = find_head(cfg); print(f"[configure] head node: {head}")
    for b in (cfg["s3_traces"], cfg["s3_results"]):
        aws(cfg, ["s3", "ls", b + "/"], check=False)
    with open(p, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)
    gi = os.path.join(repo, ".gitignore")
    line = ".aws-launch/\n"
    if not (os.path.exists(gi) and line.strip() in open(gi).read()):
        open(gi, "a").write(line)
    print(f"[configure] wrote {p} (gitignored .aws-launch/). Ready.")

def _knobs(cfg, override):
    k = override or cfg["default_knobs"]
    return " ".join(k.split()).replace("{REMOTE_REPO}", cfg["remote_repo_path"])

def boot_prefix(cfg):
    """Per-PROJECT bootstrap prefix. Never share one key across projects/users: a
    second project (or an intern) uploading its wrapper would silently replace yours."""
    return f"{cfg['s3_results']}/bootstrap/{cfg['project']}"

def results_prefix(cfg):
    """Per-PROJECT results prefix, so `collect` never mixes two projects' output."""
    return f"{cfg['s3_results']}/results/{cfg['project']}"

def render_wrapper(cfg, src):
    """The bundled wrapper is a TEMPLATE: its #SBATCH -o/-e lines cannot use a shell
    variable (Slurm parses them before any shell runs), so the project root is
    substituted here, at upload time, per project."""
    txt = open(src).read().replace("{{PROJECT_ROOT}}", cfg["remote_project_root"])
    tf = tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False)
    tf.write(txt); tf.close()
    return tf.name

def verb_submit(args):
    repo = args.repo; cfg = load_cfg(repo)
    traces = [t.strip() for t in open(args.traces) if t.strip()]
    exps = {}
    for spec in args.exps.split(";"):
        name, _, knobs = spec.partition("=")
        exps[name.strip()] = _knobs(cfg, knobs.strip() or None)
    if not traces or not exps:
        sys.exit("error_id=empty_spec")
    batch = args.label or time.strftime("batch-%Y%m%d-%H%M%S")
    print(f"[submit] batch={batch} traces={len(traces)} exps={list(exps)} -> {len(traces)*len(exps)} jobs")
    head = wake(cfg); print(f"[submit] head={head} awake")

    # stage job wrapper + trace list to S3
    wrapper = render_wrapper(cfg, os.path.join(SKILL_DIR, cfg["job_wrapper"]))
    s3_put(cfg, wrapper, f"{boot_prefix(cfg)}/champsim-job.sh")
    tl = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
    tl.write("\n".join(traces) + "\n"); tl.close()
    s3_put(cfg, tl.name, f"{boot_prefix(cfg)}/{batch}.traces.txt")

    # ensure built + smoke-gate (run one quick job on the head node itself)
    st, out, err = ssm_run(cfg, head, [
        f"runuser -l {cfg['remote_user']} -c 'set -e; cd {cfg['remote_repo_path']}; "
        f"[ -x {cfg['binary']} ] || {cfg['build_command']}; echo BUILT=$(test -x {cfg['binary']} && echo yes || echo no)'"],
        timeout=900)
    if "BUILT=yes" not in out:
        sys.exit(f"error_id=build_failed\n{out}\n{err}")
    print("[submit] binary present; running smoke job...")
    smoke_knobs = "--warmup_instructions=1000000 --simulation_instructions=1000000 --trace_version=2 " \
                  f"--llc_replacement_type=ship --config={cfg['remote_repo_path']}/config/nopref.ini " \
                  "--num_rob_partitions=3 --rob_partition_size=64,128,320 --rob_frontal_partition_ids=0 --rob_dorsal_partition_ids=2"
    st, out, err = ssm_run(cfg, head, [
        f"runuser -l {cfg['remote_user']} -c 'cd {cfg['remote_repo_path']}; mkdir -p /tmp/smoke; "
        f"aws s3 cp {cfg['s3_traces']}/{cfg['trace_prefix']}/{traces[0]} /tmp/smoke/t.zst --region {cfg['region']} --no-progress >/dev/null; "
        f"{cfg['binary']} {smoke_knobs} -traces /tmp/smoke/t.zst 2>&1 | grep -c \"Finished CPU 0\"; rm -f /tmp/smoke/t.zst'"],
        timeout=600)
    if "1" not in out.split():
        sys.exit(f"error_id=smoke_failed (no Finished line)\n{out}\n{err}")
    print("[submit] smoke passed. Submitting array...")

    # remote submit: upload a bundled submitter script + trace/exps files, run it.
    # Knobs live in the exps file (never inline) -> no SSM quoting to get wrong.
    ef = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
    for exp, knobs in exps.items():
        ef.write(f"{exp}\t{knobs}\n")
    ef.close()
    s3_put(cfg, ef.name, f"{boot_prefix(cfg)}/{batch}.exps.txt")
    s3_put(cfg, os.path.join(SKILL_DIR, "scripts", "_submit_remote.sh"),
           f"{boot_prefix(cfg)}/_submit_remote.sh")
    BOOT, R, PR = boot_prefix(cfg), cfg["region"], cfg["remote_project_root"]
    wall = cfg.get("walltime", "24:00:00")
    # everything lands in the PROJECT's run-assets, never $HOME
    runuser_cmd = (
        f"runuser -l {cfg['remote_user']} -c 'set -e; mkdir -p {PR}/run-assets; cd {PR}/run-assets; "
        f"aws s3 cp {BOOT}/_submit_remote.sh sb.sh --region {R} --no-progress; "
        f"aws s3 cp {BOOT}/{batch}.traces.txt traces.txt --region {R} --no-progress; "
        f"aws s3 cp {BOOT}/{batch}.exps.txt exps.txt --region {R} --no-progress; "
        f"bash sb.sh {BOOT} {PR} traces.txt exps.txt {cfg['partition']} "
        f"{cfg['ncores_per_job']} {wall} {R}'")
    st, out, err = ssm_run(cfg, head, [runuser_cmd], timeout=600)
    jobs = []
    for m in re.finditer(r"^SUBMIT (\S+) (\S+) (\d+)$", out, re.M):
        jobs.append({"exp": m.group(1), "trace": m.group(2), "job_id": m.group(3),
                     "tag": f"{m.group(1)}:{m.group(2)}"})
    if not jobs:
        sys.exit(f"error_id=submit_failed (no job ids)\n{out}\n{err}")
    os.makedirs(os.path.join(runs_dir(repo, cfg), batch), exist_ok=True)
    with open(ledger_path(repo, cfg, batch), "w") as f:
        json.dump({"batch": batch, "state": "submitted", "submitted_at": time.time(),
                   "n_jobs": len(jobs), "jobs": jobs}, f, indent=2)
    print(f"[submit] OK — batch {batch}: {len(jobs)} jobs queued.")

def verb_status(args):
    repo = args.repo; cfg = load_cfg(repo)
    batch = args.batch or _latest_batch(repo, cfg)
    led = json.load(open(ledger_path(repo, cfg, batch)))
    head = wake(cfg)
    # ParallelCluster runs Slurm with accounting OFF (no sacct), so detect
    # completion via squeue membership: a job in squeue is active; absent => done.
    st, out, err = ssm_run(cfg, head, [
        f"export PATH=/opt/slurm/bin:$PATH; "
        f"echo QSTART; squeue -h -o '%i %T' 2>/dev/null; echo QEND; "
        f"echo NODES=$(sinfo -h -o '%D %t' | awk '$2==\"mix\"||$2==\"alloc\"||$2==\"idle\"{{s+=$1}}END{{print s+0}}')"],
        timeout=120)
    active, in_q = {}, False
    for line in out.splitlines():
        t = line.strip()
        if t == "QSTART": in_q = True; continue
        if t == "QEND": in_q = False; continue
        if in_q:
            p = t.split()
            if len(p) >= 2 and p[0].isdigit():
                active[p[0]] = p[1]
    counts = {}
    for j in led["jobs"]:
        s = active.get(j["job_id"], "DONE")
        counts[s] = counts.get(s, 0) + 1
    done = all(j["job_id"] not in active for j in led["jobs"])
    led["state"] = "complete" if done else "running"
    json.dump(led, open(ledger_path(repo, cfg, batch), "w"), indent=2)
    nodes = re.search(r"NODES=(\d+)", out)
    print(f"[status] batch={batch} state={led['state']}  " +
          "  ".join(f"{k}={v}" for k, v in sorted(counts.items())) +
          (f"  spot_nodes={nodes.group(1)}" if nodes else ""))

def verb_collect(args):
    repo = args.repo; cfg = load_cfg(repo)
    batch = args.batch
    led = json.load(open(ledger_path(repo, cfg, batch)))
    if led.get("state") != "complete" and not args.force:
        sys.exit("batch not complete (run status; use --force to collect anyway)")
    outdir = os.path.join(runs_dir(repo, cfg), batch); os.makedirs(outdir, exist_ok=True)
    # results are s3://<results>/results/<trace_stem>-<exp>-j<jid>.txt
    aws(cfg, ["s3", "sync", f"{results_prefix(cfg)}/", os.path.join(outdir, "raw"),
              "--exclude", "*", "--include", "*-j*.txt", "--no-progress"], check=False)
    rows = []
    for j in led["jobs"]:
        stem = j["trace"].replace(".champsim2.zst", "")
        f = os.path.join(outdir, "raw", f"{stem}-{j['exp']}-j{j['job_id']}.txt")
        ipc = None
        if os.path.exists(f):
            for line in open(f, errors="replace"):
                m = re.search(r"Finished CPU 0 .*cumulative IPC:\s*([0-9.]+)", line)
                if m: ipc = float(m.group(1))
        rows.append((j["trace"], j["exp"], ipc))
    csv = os.path.join(outdir, "summary.csv")
    with open(csv, "w") as f:
        f.write("trace,exp,ipc\n")
        for t, e, i in rows:
            f.write(f"{t},{e},{'' if i is None else i}\n")
    ok = sum(1 for _, _, i in rows if i is not None)
    print(f"[collect] batch={batch}: {ok}/{len(rows)} results parsed -> {csv}")

def _latest_batch(repo, cfg):
    d = runs_dir(repo, cfg)
    bs = [b for b in os.listdir(d)] if os.path.isdir(d) else []
    if not bs: sys.exit("no batches found")
    return sorted(bs)[-1]


def main():
    ap = argparse.ArgumentParser(prog="aws_launch.py")
    sub = ap.add_subparsers(dest="verb", required=True)
    for v in ("configure", "submit", "status", "collect"):
        s = sub.add_parser(v); s.add_argument("--repo", required=True)
    # per-verb args
    sub.choices["configure"].add_argument("--profile"); sub.choices["configure"].add_argument("--cluster")
    sub.choices["configure"].add_argument("--force", action="store_true")
    sub.choices["submit"].add_argument("--traces", required=True)
    sub.choices["submit"].add_argument("--exps", required=True,
        help='semicolon-separated name=knobs; empty knobs -> default_knobs. e.g. "nopref=;pythia=--l2c_prefetcher_types=scooby ..."')
    sub.choices["submit"].add_argument("--label")
    sub.choices["status"].add_argument("--batch")
    sub.choices["collect"].add_argument("--batch", required=True)
    sub.choices["collect"].add_argument("--force", action="store_true")
    args = ap.parse_args()
    {"configure": verb_configure, "submit": verb_submit,
     "status": verb_status, "collect": verb_collect}[args.verb](args)

if __name__ == "__main__":
    main()
