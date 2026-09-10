#!/usr/bin/env python3
"""aws-launch reference backend (ChampSim/Hermes).

Implements the verbs the aws-launch skill drives: configure, sync, submit,
status, collect. Runs LOCALLY on the user's machine; drives the AWS ParallelCluster head
node over SSM and moves inputs/results through S3. All AWS access uses the named
profile from config (never raw keys).

Usage:
  aws_launch.py configure --repo <root> [--profile P --cluster C --force]
  aws_launch.py sync      --repo <root> [--build]
  aws_launch.py submit    --repo <root> --traces file --exps name=knobs[;...] [--label L] [--no-sync] [--spot-smoke]
  aws_launch.py submit    --repo <root> --tlist T [T..] --exp E [E..] --mfile M [M..] [--label L] [--smoke-idx N] [--no-sync]
  aws_launch.py status    --repo <root> [--batch B]
  aws_launch.py collect   --repo <root> --batch B [--force]

Requires: python3 + PyYAML + AWS CLI v2 locally. See reference/credentials.md.
"""
import argparse, collections, contextlib, csv, hashlib, json, os, posixpath, re, shlex, stat, subprocess, sys, tarfile, tempfile, time
try:
    import yaml
except ImportError:
    sys.exit("ERROR: PyYAML required locally (pip install pyyaml)")

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCH_TIMEOUT_S = 7200   # a head-node launch: smoke trace fetch + smoke run + one sbatch per job
# `runuser -c` starts a new session, which SSM's executionTimeout kill need not reach, so a launch bounds itself
# this much earlier (staging before it, the kill grace and the record uploads after it).
LAUNCH_MARGIN_S = 300
SYNC_LOCK_WAIT_S = 900    # how long a sync waits for the project lock


# ---------- config / ledger ----------
def cfg_path(repo): return os.path.join(repo, ".aws-launch", "config.yml")

PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*$")

def validate_cfg(cfg, p):
    """Enforce the shared-cluster convention. Every check here corresponds to a way two
    people or two projects can silently corrupt each other's run; none of them fail loudly
    on their own, which is why they are checked up front rather than discovered later."""
    missing = [k for k in ("project", "remote_project_root") if not cfg.get(k)]
    if missing:
        sys.exit(f"ERROR: {p} is missing {missing}.\n"
                 "This config predates per-project namespacing. Add:\n"
                 "  project: <owner>/<project>            e.g. rbera/hermes-uncore\n"
                 "  remote_project_root: /home/<user>/<owner>/<project>\n"
                 "and point remote_repo_path at <remote_project_root>/Hermes.\n"
                 "See reference/config-template.yml.")
    proj, root = cfg["project"], cfg["remote_project_root"].rstrip("/")
    repo = cfg.get("remote_repo_path", "")

    if "<" in proj or ">" in proj or "<" in root or ">" in root:
        sys.exit(f"ERROR: {p} still contains template placeholders "
                 f"(project={proj!r}, remote_project_root={root!r}).\n"
                 "Replace them with your real owner/project before running anything.")
    if not PROJECT_RE.match(proj):
        sys.exit(f"ERROR: project={proj!r} must be OWNER-FIRST '<owner>/<project>', "
                 "exactly one '/', lowercase (e.g. rbera/hermes-uncore).\n"
                 "The S3 layout is <bucket>/<owner>/<project>/... so that ONE IAM statement\n"
                 "(<bucket>/<owner>/*) scopes a person. A bare name would create a\n"
                 "top-level prefix outside anyone's namespace.")
    if not root.startswith("/"):
        sys.exit(f"ERROR: remote_project_root={root!r} must be an absolute path.")
    if not root.endswith("/" + proj):
        sys.exit(f"ERROR: remote_project_root={root!r} must end with '{proj}'.\n"
                 f"  expected something like /home/ubuntu/{proj}\n"
                 "Otherwise results land under one owner's S3 prefix while files are written\n"
                 "into a different owner's directory on the head node -- silently.")
    if repo and not repo.startswith(root + "/"):
        sys.exit(f"ERROR: remote_repo_path={repo!r} must live under remote_project_root\n"
                 f"  ({root}/...), so each project builds its OWN ChampSim.")
    return cfg

def load_cfg(repo):
    p = cfg_path(repo)
    if not os.path.exists(p):
        sys.exit(f"ERROR: no config at {p} — run `configure` (bootstrap) first")
    with open(p) as f:
        cfg = yaml.safe_load(f)
    cfg = validate_cfg(cfg, p)
    # Added with the jobfile route; configs written before it get the layout defaults.
    cfg.setdefault("remote_infra_path", cfg["remote_project_root"].rstrip("/") + "/champsim-infra")
    cfg.setdefault("trace_cache_dir", "/scratch/trace_cache")
    # Added with the locked snapshot: the dirs under remote_repo_path a batch freezes besides its binary, and
    # how long a head-node launch may run.
    cfg.setdefault("snapshot_dirs", ["config"])
    cfg.setdefault("launch_timeout_s", LAUNCH_TIMEOUT_S)
    return cfg

def runs_dir(repo, cfg): return os.path.join(repo, cfg.get("runs_base", ".aws-launch/runs"))

def ledger_path(repo, cfg, batch): return os.path.join(runs_dir(repo, cfg), batch, "ledger.json")

def write_json(path, obj):
    """Replace `path` with `obj` in one step -- temp file in the same dir, fsync, os.replace -- so an interrupted or
    failed write leaves the old record, never a truncated one. Every ledger and attempt record goes through here."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=f".{os.path.basename(path)}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(obj, indent=2) + "\n"); f.flush(); os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


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
    """Run shell lines on the head node via SSM run-command; return (status, stdout, stderr). `timeout` is also
    the command's executionTimeout: TimeoutSeconds only bounds delivery, and AWS-RunShellScript otherwise stops
    every command at 3600 s however long this side waits."""
    spec = {"InstanceIds": [head], "DocumentName": "AWS-RunShellScript",
            "Parameters": {"commands": lines, "executionTimeout": [str(timeout)]}, "TimeoutSeconds": timeout}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
        json.dump(spec, tf); inp = tf.name
    try:
        cid = aws(cfg, ["ssm", "send-command", "--cli-input-json", f"file://{inp}",
                        "--query", "Command.CommandId", "--output", "text"])
        for _ in range(max(1, (timeout + 120) // poll)):   # + delivery and start-up
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

def s3_get(cfg, key, local):
    """Fetch one S3 object. Used for records too big to survive SSM's 24,000-char
    stdout cap -- see the job-id read in verb_submit. aws() raises on failure."""
    aws(cfg, ["s3", "cp", key, local, "--no-progress"])
    return local

def s3_get_retry(cfg, key, local, tries=3, wait=5):
    """s3_get for a record of queued job ids: one failed call must not lose them."""
    for i in range(tries):
        try:
            return s3_get(cfg, key, local)
        except Exception:
            if i + 1 == tries:
                raise
            time.sleep(wait * (i + 1))

# ---------- source sync ----------
# The head node builds whatever sits in remote_repo_path. Unsynced, `submit` smoke-tests and
# launches a checkout of unknown age and nothing fails. So the local work trees are mirrored onto
# the head node, checked file by file against a sha256 manifest and built -- in one SSM command
# under one per-project lock. S3 is the transport because the head node has no SSH.

SYNC_RESERVED = ("results", "run-assets")                # layout dirs a mirror must never replace
SAFE_REMOTE_PATH = re.compile(r"^/[A-Za-z0-9._@+/-]+$")  # interpolated into remote shells

SYNC_APPLY_SH = r"""#!/bin/bash
# aws-launch sync, head-node side: mirror each tree of the bundle into the project root, verify
# it, run build.sh and then snapshot.sh if the bundle has them. One lock spans all of it, so a
# concurrent sync of the same project can neither interleave nor change a tree between its check,
# its build and a batch's copy of both.
# Usage: apply.sh <bundle dir> <remote_project_root>
set -euo pipefail
W=$1; ROOT=$2
exec 9>"/tmp/aws-launch-sync$(printf %s "$ROOT" | tr / _).lock"
flock -w @LOCK_WAIT@ 9 || { echo "SYNC_LOCK_TIMEOUT $ROOT"; exit 3; }
# a label already used refuses before anything is mirrored or built
if [ -f "$W/snapshot.sh" ]; then bash "$W/snapshot.sh" "$W" check; fi
realroot=$(realpath -m -- "$ROOT")
while IFS=$'\t' read -r name remote protect; do
  [ -n "$name" ] || continue
  src="$W/tree.$name"; dst="$ROOT/$remote"
  # rsync --delete follows a symlinked destination: on a shared node it would mirror into, and
  # delete from, whatever the link points at.
  realdst=$(realpath -m -- "$dst")
  if [ -L "$dst" ] || [ "${realdst#"$realroot"/}" = "$realdst" ]; then
    echo "SYNC_UNSAFE_DST $name $dst -> $realdst"; exit 4
  fi
  mkdir -p "$src" "$dst"
  tar -xzf "$W/$name.tar.gz" -C "$src"
  IFS=',' read -r -a prot <<< "$protect"
  ex=()
  for p in "${prot[@]}"; do ex+=("--exclude=/$p"); done
  gone=$(rsync -a --delete --checksum --dry-run --itemize-changes "${ex[@]}" "$src/" "$dst/" \
         | sed -n 's/^\*deleting *//p')
  n=$(printf '%s' "$gone" | grep -c '' || true)
  top=$(printf '%s\n' "$gone" | awk -F/ 'NF{c[$1]++} END{for (k in c) print c[k], k}' \
        | sort -rn | awk 'NR<=8{printf "%s:%s ", $2, $1}')
  echo "SYNC_DELETED $name $n $top"
  rsync -a --delete --checksum "${ex[@]}" "$src/" "$dst/"
  python3 "$W/verify.py" "$dst" "$W/$name.manifest" "$name" "${prot[@]}"
done < "$W/entries.tsv"
if [ -f "$W/build.sh" ]; then bash "$W/build.sh"; fi
if [ -f "$W/snapshot.sh" ]; then bash "$W/snapshot.sh" "$W" take; fi
""".replace("@LOCK_WAIT@", str(SYNC_LOCK_WAIT_S))

SYNC_BUILD_SH = r"""#!/bin/bash
# aws-launch build, head-node side. The old binary stays until the build replaces it, and only a
# binary newer than this run counts as built. @KEEP@ is `:`, or (--no-sync) keeps an
# existing binary as it is.
set -uo pipefail
cd @REPO@ || { echo "BUILT=no cannot-cd"; exit 5; }
@KEEP@
M=$(mktemp /tmp/aws-launch-build.XXXXXX)
( @BUILD@ ) > @LOG@ 2>&1; rc=$?
if [ "$rc" -eq 0 ] && [ -x @BINARY@ ] && [ @BINARY@ -nt "$M" ]; then
  echo "BUILT=yes $(sha256sum < @BINARY@ | cut -c1-64)"; rm -f "$M"
else
  echo "BUILT=no rc=$rc"; tail -40 @LOG@; rm -f "$M"; exit 6
fi
"""

SYNC_VERIFY_PY = r"""import hashlib, os, stat, sys
# Recompute the mirrored tree's manifest rows and compare them with the shipped manifest. With
# --prefix=P, dst is a copy of the tree's P/ and only the manifest rows under P/ count.
# Usage: verify.py <dst> <manifest> <name> [--prefix=P] [protect...]
dst, manifest, name = sys.argv[1], sys.argv[2], sys.argv[3]
opts = sys.argv[4:]
prefix = next((a[len("--prefix="):] for a in opts if a.startswith("--prefix=")), "").strip("/")
protect = [p.strip("/") for p in opts if not p.startswith("--prefix=") and p.strip("/")]
if prefix:
    protect = [p[len(prefix) + 1:] for p in protect if p.startswith(prefix + "/")]
if os.path.islink(dst) or not os.path.isdir(dst):
    print(f"SYNC_VERIFY_FAIL {name} destination {dst} is a symlink or not a directory")
    sys.exit(1)
def under(p, pre):
    return any(p == q or p.startswith(q + "/") for q in pre)
def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()
want = {}
with open(manifest, encoding="utf-8", errors="surrogateescape") as f:
    for line in f:
        if line.startswith("#") or not line.strip():
            continue
        kind, mode, path = line.rstrip("\n").split("\t", 2)
        if prefix:
            if not path.startswith(prefix + "/"):
                continue
            path = path[len(prefix) + 1:]
        want[path] = f"{kind}\t{mode}\t{path}"
have = {}
for d, dirs, files in os.walk(dst):
    rd = os.path.relpath(d, dst)
    rd = "" if rd == "." else rd
    keep = []
    for n in dirs:
        rel = f"{rd}/{n}" if rd else n
        if under(rel, protect):
            continue
        if os.path.islink(os.path.join(d, n)):
            files.append(n)          # a symlinked directory is a leaf, not a subtree
        else:
            keep.append(n)
    dirs[:] = keep
    for n in files:
        rel = f"{rd}/{n}" if rd else n
        if under(rel, protect):
            continue
        full = os.path.join(d, n)
        st = os.lstat(full)
        if stat.S_ISLNK(st.st_mode):
            have[rel] = f"link:{os.readlink(full)}\t-\t{rel}"
        elif stat.S_ISREG(st.st_mode):
            have[rel] = f"{sha(full)}\t{'x' if st.st_mode & 0o111 else '-'}\t{rel}"
        else:
            have[rel] = f"special\t-\t{rel}"
missing = sorted(set(want) - set(have))
extra = sorted(set(have) - set(want))
changed = sorted(p for p in set(want) & set(have) if want[p] != have[p])
digest = hashlib.sha256("\n".join(have[p] for p in sorted(have)).encode("utf-8", "surrogateescape")).hexdigest()
if missing or extra or changed:
    print(f"SYNC_VERIFY_FAIL {name} missing={len(missing)} extra={len(extra)} "
          f"changed={len(changed)} e.g. {missing[:3] + extra[:3] + changed[:3]}")
    sys.exit(1)
print(f"SYNC_VERIFY_OK {name} {len(have)} {digest}")
"""

SNAPSHOT_SH = r"""#!/bin/bash
# aws-launch batch snapshot, head-node side. apply.sh runs `check` before it mirrors anything and `take`
# after the build, both inside the project lock, so the batch freezes exactly what this run verified and
# built. `take` copies the binary (a hard link shares its inode with any build that writes in place),
# refuses a copied symlink that leads out of the batch, re-verifies every copied tree against its sync
# manifest, and prints both hashes of the binary, so a writer that ignores the lock shows up too.
# Usage: snapshot.sh <bundle dir> check|take
set -uo pipefail
W=$1; MODE=$2; A=@DEST@; SRC=@BINARY@; EXE=@EXE@; TOKEN=@TOKEN@
if [ -e "$A" ] || [ -L "$A" ]; then echo "SNAPSHOT_EXISTS $A"; exit 13; fi
[ "$MODE" = take ] || exit 0
[ -f "$SRC" ] && [ -x "$SRC" ] || { echo "SNAPSHOT_NO_BINARY $SRC"; exit 14; }
mkdir -p "$(dirname "$A")" && mkdir "$A" "$A/bin" || { echo "SNAPSHOT_FAILED mkdir $A"; exit 15; }
RA=$(realpath -m -- "$A")
cp -pL "$SRC" "$A/$EXE.tmp" && mv "$A/$EXE.tmp" "$A/$EXE" || { echo "SNAPSHOT_FAILED binary"; exit 15; }
echo "SNAPSHOT_BIN $(sha256sum < "$SRC" | cut -c1-64) $(sha256sum < "$A/$EXE" | cut -c1-64)"
# rows: src, dst, required, manifest, prefix, protect -- "-" for empty, since tabs in IFS collapse
while IFS=$'\t' read -r src dst required manifest prefix protect; do
  [ -n "$src" ] || continue
  if [ ! -e "$src" ] && [ ! -L "$src" ]; then
    if [ "$required" = 1 ]; then echo "SNAPSHOT_FAILED $dst: $src does not exist"; exit 15; fi
    echo "SNAPSHOT_SKIPPED $dst"; continue
  fi
  if [ -L "$src" ] || [ ! -d "$src" ]; then echo "SNAPSHOT_FAILED $dst: $src is a symlink or not a directory"; exit 15; fi
  mkdir -p "$(dirname "$A/$dst")" && cp -a "$src" "$A/$dst" || { echo "SNAPSHOT_FAILED copy $dst"; exit 15; }
  # cp -a keeps symlinks, and one leading out of the batch would still read the live tree
  while IFS= read -r -d '' l; do
    t=$(realpath -m -- "$l" 2>/dev/null)
    case "$t" in "$RA"/*) ;; *) echo "SNAPSHOT_FAILED $dst: symlink ${l#"$A"/} leads out of the batch, to ${t:-an unresolvable target}; replace it with the file"; exit 15;; esac
  done < <(find "$A/$dst" -type l -print0)
  if [ "$manifest" = - ]; then echo "SNAPSHOT_UNVERIFIED $dst"; continue; fi
  [ "$prefix" = - ] && prefix=
  [ "$protect" = - ] && protect=
  IFS=',' read -r -a prot <<< "$protect"
  python3 "$W/verify.py" "$A/$dst" "$W/$manifest" "snapshot:$dst" "--prefix=$prefix" "${prot[@]}" || exit 16
done < "$W/snapshot.tsv"
printf '%s\n' "$TOKEN" > "$A/.aws-launch-snapshot" || { echo "SNAPSHOT_FAILED token"; exit 15; }
echo "SNAPSHOT_OK $A"
"""

def _git_toplevel(path):
    r = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True)
    return os.path.realpath(r.stdout.strip()) if r.returncode == 0 else None

def _rel_path(p, what):
    s = str(p or "").strip()
    parts = [c for c in s.split("/") if c not in ("", ".")]
    if s.startswith("/") or not parts or ".." in parts or any(c in s for c in ",\t\n"):
        sys.exit(f"error_id=bad_sync_config — {what} {p!r} must be a relative path below its "
                 "root, without '..', commas, tabs or newlines.")
    return "/".join(parts)

def _under(path, prefixes):
    return any(path == q or path.startswith(q + "/") for q in prefixes)

def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def default_sync(repo, cfg):
    """Propose the tree that builds remote_repo_path: <repo>/<basename> when that is a git work tree,
    else the repo itself -- and only a tree holding the build script, so a workspace repo is never
    mistaken for the simulator."""
    name = os.path.basename(cfg["remote_repo_path"].rstrip("/"))
    entry = (shlex.split(cfg.get("build_command") or "") or [""])[0]
    for local in (name, "."):
        path = os.path.realpath(os.path.join(repo, local))
        if _git_toplevel(path) == path and ("/" not in entry or os.path.exists(os.path.join(path, entry))):
            return [{"local": local, "remote": name, "protect": ["libbf", "bin", "obj"]}]
    return []

def sync_entries(repo, cfg):
    """Resolve and validate `sync` before anything wakes or uploads. Mirroring deletes, so there is
    no silent default: a config without `sync:` gets a proposal to review instead."""
    if "sync" not in cfg:
        prop = default_sync(repo, cfg)
        hint = (yaml.safe_dump({"sync": prop}, sort_keys=False) if prop else
                "sync:\n- local: <git work tree, relative to the repo>\n  remote: <dir under remote_project_root>\n")
        sys.exit(f"error_id=sync_not_configured — {cfg_path(repo)} has no `sync:` list. Sync mirrors "
                 "with --delete, so it is opt-in. Review this, add it to the config, and re-run:\n" + hint)
    for k in ("remote_project_root", "remote_repo_path"):
        if not SAFE_REMOTE_PATH.match(str(cfg.get(k) or "")):
            sys.exit(f"error_id=bad_sync_config — {k}={cfg.get(k)!r} may use only letters, digits and "
                     "._@+-/ because it is passed to a remote shell.")
    if not cfg["sync"]:
        sys.exit(f"error_id=no_sync_source — the `sync:` list in {cfg_path(repo)} is empty.")
    entries = []
    for i, e in enumerate(cfg["sync"]):
        local = os.path.realpath(os.path.join(repo, str(e.get("local") or ".")))
        if _git_toplevel(local) != local:
            sys.exit(f"error_id=bad_sync_config — sync[{i}].local resolves to {local}, which is "
                     "not the root of a git work tree. Sync ships what git tracks, so it needs one.")
        remote = _rel_path(e.get("remote"), f"sync[{i}].remote")
        if _under(remote, SYNC_RESERVED):
            sys.exit(f"error_id=bad_sync_config — sync[{i}].remote {remote!r} would mirror over "
                     "the project's results/ or run-assets/ and delete what is there.")
        name = re.sub(r"[^A-Za-z0-9._-]", "_", remote)
        for o in entries:
            if _under(remote, [o["remote"]]) or _under(o["remote"], [remote]) or o["name"] == name:
                sys.exit(f"error_id=bad_sync_config — sync remotes {o['remote']!r} and {remote!r} "
                         "overlap; mirroring one would delete the other.")
        entries.append({"name": name, "local": local, "remote": remote,
                        "protect": [_rel_path(x, f"sync[{i}].protect") for x in e.get("protect") or []],
                        "exclude": [_rel_path(x, f"sync[{i}].exclude") for x in e.get("exclude") or []]})
    return entries

def pack_entry(e, outdir):
    """Tar one tree's source set -- tracked files plus untracked files git does not ignore, as
    they are on disk -- and write its manifest. Returns the provenance record."""
    ls = subprocess.run(["git", "-C", e["local"], "ls-files", "-z", "--cached", "--others",
                         "--exclude-standard"], capture_output=True, check=True)
    rows, shipped = [], []
    for p in sorted({x for x in ls.stdout.decode("utf-8", "surrogateescape").split("\0") if x}):
        if _under(p, e["exclude"]) or _under(p, [".aws-launch"]):
            continue
        full = os.path.join(e["local"], p)
        if not os.path.lexists(full):          # deleted in the work tree, still in the index
            continue
        if _under(p, e["protect"]):
            sys.exit(f"error_id=bad_sync_config — protect covers {p!r}, which {e['local']} ships; "
                     "it would never reach the head node.")
        st = os.lstat(full)
        if stat.S_ISLNK(st.st_mode):
            row = f"link:{os.readlink(full)}\t-\t{p}"
        elif stat.S_ISREG(st.st_mode):
            row = f"{_sha256_file(full)}\t{'x' if st.st_mode & 0o111 else '-'}\t{p}"
        else:
            sys.exit(f"error_id=bad_sync_path — {e['local']}/{p} is not a file or symlink "
                     "(a submodule?); sync does not support it.")
        if row.count("\t") != 2 or "\n" in row:
            sys.exit(f"error_id=bad_sync_path — {e['local']}/{p!r} has a tab or newline in its "
                     "name or link target.")
        rows.append(row); shipped.append(p)
    digest = hashlib.sha256("\n".join(rows).encode("utf-8", "surrogateescape")).hexdigest()
    git = lambda *a: subprocess.run(["git", "-C", e["local"], *a], capture_output=True, text=True).stdout
    dirty = [l for l in git("status", "--porcelain").splitlines()
             if not _under(l[3:].rstrip("/"), e["exclude"] + [".aws-launch"])]
    with tarfile.open(os.path.join(outdir, e["name"] + ".tar.gz"), "w:gz") as tf:
        for p in shipped:
            tf.add(os.path.join(e["local"], p), arcname=p, recursive=False)
    rec = {"local": e["local"], "remote": e["remote"], "git_head": git("rev-parse", "HEAD").strip(),
           "dirty": dirty, "files": len(rows), "digest": digest}
    with open(os.path.join(outdir, e["name"] + ".manifest"), "w",
              encoding="utf-8", errors="surrogateescape") as f:
        f.write("# aws-launch sync manifest v1\n")
        f.write("".join(f"# {k} {rec[k]}\n" for k in ("local", "remote", "git_head", "files", "digest")))
        f.write("".join(f"# dirty {l}\n" for l in dirty))
        f.write("".join(r + "\n" for r in rows))
    return rec

def sync_push(cfg, repo, head, entries, build=False, snapshot=None):
    """Pack, upload, mirror and verify every entry, build if asked, and freeze a batch -- one SSM command under
    one per-project lock, so no other sync of the project lands between the build and the batch's copy of it.
    `build` is True (rebuild) or "if-missing" (keep an existing binary); `snapshot` is a snapshot_plan. Exits
    unless every head-node digest equals the local one, any build produced a binary newer than this run, and
    the snapshot matches what this run verified and built."""
    stamp = attempt_stamp()
    local_dir = os.path.join(repo, ".aws-launch", "sync", stamp)
    os.makedirs(local_dir)
    recs = {}
    for e in entries:
        recs[e["name"]] = r = pack_entry(e, local_dir)
        print(f"[sync] {r['local']} -> {e['remote']} (protect {e['protect']}): {r['files']} files, "
              f"git {r['git_head'][:10]}" + (f", {len(r['dirty'])} uncommitted change(s) included" if r["dirty"] else ""))
    with open(os.path.join(local_dir, "entries.tsv"), "w") as f:
        f.write("".join(f"{e['name']}\t{e['remote']}\t{','.join(e['protect'])}\n" for e in entries))
    with open(os.path.join(local_dir, "apply.sh"), "w") as f:
        f.write(SYNC_APPLY_SH)
    with open(os.path.join(local_dir, "verify.py"), "w") as f:
        f.write(SYNC_VERIFY_PY)
    log = f"/tmp/aws-launch-build.{stamp}.log"
    if build:
        keep = ('if [ -f @BINARY@ ] && [ -x @BINARY@ ]; then echo "BUILT=kept $(sha256sum < @BINARY@ | cut -c1-64)"; '
                'exit 0; fi' if build == "if-missing" else ":")
        with open(os.path.join(local_dir, "build.sh"), "w") as f:
            f.write(SYNC_BUILD_SH.replace("@KEEP@", keep).replace("@REPO@", shlex.quote(cfg["remote_repo_path"]))
                    .replace("@BINARY@", shlex.quote(cfg["binary"])).replace("@LOG@", shlex.quote(log))
                    .replace("@BUILD@", cfg["build_command"]))
    expect = snapshot_bundle(cfg, snapshot, entries, local_dir) if snapshot else {}
    key = f"{boot_prefix(cfg)}/sync/{stamp}"
    aws(cfg, ["s3", "cp", "--recursive", local_dir + "/", key + "/", "--no-progress", "--only-show-errors"])
    for e in entries:                           # S3 holds the tarballs; keep the manifests here
        os.unlink(os.path.join(local_dir, e["name"] + ".tar.gz"))
    U, R, ROOT = cfg["remote_user"], cfg["region"], cfg["remote_project_root"].rstrip("/")
    steps = ((["mirroring"] if entries else []) + (["rebuilding"] if build is True else ["building if no binary"] if build else [])
             + ([f"freezing {snapshot['dest']}"] if snapshot else []))
    print(f"[sync] {', '.join(steps)} on the head node, under the project lock...")
    st, out, err = ssm_run(cfg, head, [
        f"runuser -l {U} -c 'W=$(mktemp -d /tmp/aws-launch-sync.XXXXXX) && "
        f"aws s3 cp --recursive {key}/ $W/ --region {R} --no-progress --only-show-errors && "
        f"bash $W/apply.sh $W \"{ROOT}\"; rc=$?; rm -rf $W; exit $rc'"],
        timeout=SYNC_LOCK_WAIT_S + (900 if build else 600))   # past the lock wait, so SYNC_LOCK_TIMEOUT can report
    if st == "TimedOut":
        sys.exit("error_id=sync_timeout — the head-node command did not finish in time and may still be "
                 "running; do not re-run until it has ended. Nothing is known to be built."
                 + (f" It may still freeze {snapshot['dest']}: a batch dir may appear there under this label, spending "
                    "it, although nothing is queued from it." if snapshot else ""))
    unsafe = re.search(r"^SYNC_UNSAFE_DST (\S+) (.*)$", out, re.M)
    if unsafe:
        sys.exit(f"error_id=sync_unsafe_destination — {unsafe.group(1)}: {unsafe.group(2)} is, or passes "
                 "through, a symlink out of the project root. The sync stopped there; nothing was built.")
    if "SYNC_LOCK_TIMEOUT" in out:
        sys.exit(f"error_id=sync_locked — another sync of this project held the head-node lock for "
                 f"{SYNC_LOCK_WAIT_S // 60} min. Nothing was synced, built or frozen.")
    if snapshot and re.search(r"^SNAPSHOT_EXISTS ", out, re.M):
        sys.exit(f"error_id={snapshot['exists_error']} — {snapshot['dest']} already exists on the head node: an earlier "
                 "batch or attempt used this label, and it MAY have queued jobs (check squeue). Nothing was synced, "
                 "built or queued; pick a new --label.")
    for m in re.finditer(r"^SYNC_DELETED (\S+) (\d+) ?(.*)$", out, re.M):
        if m.group(2) != "0":
            print(f"[sync] {m.group(1)}: removed {m.group(2)} path(s) not in the source: {m.group(3).strip()}")
    got = {m.group(1): m.group(2)
           for m in re.finditer(r"^SYNC_VERIFY_OK (\S+) \d+ ([0-9a-f]{64})$", out, re.M)}
    bad = [n for n, r in recs.items() if got.get(n) != r["digest"]]
    if bad:
        sys.exit(f"error_id=sync_verify_failed — the head node's copy of {bad} does not match the local "
                 f"tree. Nothing was built.\n{out[-1500:]}\n{err[-500:]}")
    for r in recs.values():
        print(f"[sync] verified {r['remote']}: {r['files']} files identical, digest {r['digest'][:16]}")
    rec = {"stamp": stamp, "s3": key, "entries": recs}
    if build:
        mb = re.search(r"^BUILT=(yes|kept) ([0-9a-f]{64})$", out, re.M)
        if not mb:
            sys.exit(f"error_id=build_failed — no binary newer than this run came out of the build. "
                     f"Build log on the head node: {log}\n{out[-3000:]}\n{err[-500:]}")
        rec["binary_sha256"], rec["build"] = mb.group(2), {"yes": "built", "kept": "kept"}[mb.group(1)]
        print(f"[build] OK — {cfg['binary']} sha256 {mb.group(2)[:16]}"
              + (" (already there; not rebuilt)" if mb.group(1) == "kept" else ""))
    if snapshot:
        rec["snapshot"] = check_snapshot(cfg, snapshot, expect, out, err, rec)
    if st != "Success":
        sys.exit(f"error_id=sync_failed — the head-node command ended {st}.\n{out[-1500:]}\n{err[-500:]}")
    write_json(os.path.join(local_dir, "summary.json"), rec)
    return rec

# ---------- self-contained batches ----------
# A queued job reads its binary, --config files and scripts when it STARTS. So every batch runs from its
# own snapshot of all of them, and a later sync or rebuild cannot change what its unstarted jobs run --
# which is why sync never waits for the queue to drain. The snapshot is taken inside the sync's lock right
# after the build and checked against the sync manifest, so nothing lands between what was verified and
# what was frozen. The binary is a real copy, so no later build reaches it, whether the build renames a new
# binary into place or writes over the old one.

BATCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")   # a head-node directory and an S3 key segment
INPUT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")   # staged input names reach remote shells
BINARY_RE = re.compile(r"^[A-Za-z0-9._+-]+(/[A-Za-z0-9._+-]+)*$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]*$")
SNAPSHOT_DIR_RE = BINARY_RE
SNAPSHOT_RESERVED = ("bin", "scripts", "inputs")   # a batch dir's own entries
HEAD_SCRATCH = "/scratch"   # the head node has no instance store; the jobfile smoke caches its trace here
VAR_RE = re.compile(r"\$\((.*?)\)")   # create_jobfile's $(VAR)
NAME_CH = r"[\w.+@~-]"   # continues a path name: what SAFE_REMOTE_PATH and snapshot_dirs allow, plus '~'
PATH_SEP_RE = re.compile(r"""([\s=,:;'"]+)""")   # what stands between a knob name and a path, or two paths
HOME_RE = re.compile(r"\$\{HOME\}|\$HOME(?![A-Za-z0-9_])")

def require_safe(cfg, keys, error_id):
    for k in keys:
        if not SAFE_REMOTE_PATH.match(str(cfg.get(k) or "")):
            sys.exit(f"error_id={error_id} — {k}={cfg.get(k)!r} may use only letters, digits and "
                     "._@+-/ because it is passed to a remote shell.")

def attempt_stamp():
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + os.urandom(2).hex()

def batch_layout(cfg, args, repo):
    """Name the batch and check what its remote commands interpolate -- before anything wakes. A label is
    single-use: an attempt that froze its batch may have queued jobs, so its local run dir is never reused
    or deleted."""
    batch = args.label or time.strftime("batch-%Y%m%d-%H%M%S")
    if not BATCH_RE.match(batch):
        sys.exit(f"error_id=bad_label — {batch!r} names a head-node directory and an S3 key: use letters, "
                 "digits and ._- (starting with a letter or digit), at most 100 characters.")
    require_safe(cfg, ("remote_project_root", "remote_repo_path", "remote_infra_path"), "bad_config")
    b = str(cfg.get("binary") or "")
    if not BINARY_RE.match(b) or ".." in b.split("/"):
        sys.exit(f"error_id=bad_config — binary={b!r} must be a path below remote_repo_path using letters, "
                 "digits and ._+-")
    if not USER_RE.match(str(cfg.get("remote_user") or "")):
        sys.exit(f"error_id=bad_config — remote_user={cfg.get('remote_user')!r} is not a user name.")
    dirs = cfg.get("snapshot_dirs")
    if (not isinstance(dirs, list) or len({str(d) for d in dirs}) != len(dirs)
            or any(not isinstance(d, str) or not SNAPSHOT_DIR_RE.match(d) or {".", ".."} & set(d.split("/"))
                   or d.split("/")[0] in SNAPSHOT_RESERVED or any(o != d and _under(d, [o]) for o in dirs)
                   for d in dirs)):
        sys.exit(f"error_id=bad_config — snapshot_dirs={dirs!r} must list distinct, non-nested directories below "
                 f"remote_repo_path (letters, digits, ._+-; no '.' or '..'), none named {', '.join(SNAPSHOT_RESERVED)}.")
    t = cfg.get("launch_timeout_s")
    if isinstance(t, bool) or not isinstance(t, int) or not 600 <= t <= 172800:
        sys.exit(f"error_id=bad_config — launch_timeout_s={t!r} must be whole seconds from 600 to 172800 (SSM's limit).")
    refuse_spent(repo, cfg, batch)
    return batch

def refuse_spent(repo, cfg, batch):
    d = os.path.join(runs_dir(repo, cfg), batch)
    if os.path.lexists(d):
        sys.exit(f"error_id=batch_exists — {d} already exists: "
                 + ("its ledger records this batch" if os.path.exists(os.path.join(d, "ledger.json")) else
                    "an earlier attempt of this label froze its batch and recorded no jobs. It MAY have queued "
                    f"jobs: check squeue, and run `status --batch {batch}`, which recovers its ledger from the attempt's "
                    "S3 record if one exists")
                 + ". Nothing was changed or deleted; pick a new --label.")

@contextlib.contextmanager
def label_claim(repo, cfg, batch):
    """Hold runs/<batch>.claim from before the wake until the submit records its attempt (note_attempt) or exits, so two
    submits of one label on this machine, on either route, cannot both freeze a batch and race to record it. Jobs are
    queued only after note_attempt, whose run dir then keeps the label spent."""
    d = runs_dir(repo, cfg)
    os.makedirs(d, exist_ok=True)
    claim = os.path.join(d, f"{batch}.claim")
    try:
        fd = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        try:
            held = open(claim).read().strip()
        except OSError as e:
            held = str(e)
        sys.exit(f"error_id=batch_exists — {claim} exists ({held}): another submit of this label is running on this "
                 "machine, or one was killed before it recorded its attempt. Nothing was changed. If no such submit is "
                 "running, delete the claim or pick a new --label: nothing was queued under it, though its batch dir "
                 "may exist on the head node.")
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps({"pid": os.getpid(), "claimed_at": time.time()}) + "\n")
    try:
        refuse_spent(repo, cfg, batch)   # recorded by a submit that ended after batch_layout looked
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(claim)

def _path_re(path):
    """`path` as a whole path: not the tail of a longer one (/scratch<path>), nor a sibling's stem (<path>2, <path>+2)."""
    return re.compile(f"(?<!{NAME_CH})" + re.escape(path.rstrip("/")) + f"(?!{NAME_CH})")

def point_at_snapshot(cfg, text, dest):
    """Point {REMOTE_REPO}/<d> and <remote_repo_path>/<d>, for each d in snapshot_dirs, at <dest>/<d>."""
    dirs = sorted(cfg["snapshot_dirs"], key=len, reverse=True)
    if not dirs:
        return text
    pat = (f"(?<!{NAME_CH})" + r"(?:\{REMOTE_REPO\}|" + re.escape(cfg["remote_repo_path"].rstrip("/")) + r")/("
           + "|".join(map(re.escape, dirs)) + f")(?!{NAME_CH})")
    return re.sub(pat, lambda m: f"{dest}/{m.group(1)}", text)

def canonical_path(cfg, piece, cwd):
    """The absolute path a job reads for `piece` -- {REMOTE_REPO}, $HOME, ${HOME} and ~ expanded (/home/<remote_user>),
    '//' and '/./' collapsed, '..' resolved, a relative path taken from the job's `cwd` -- or None if it is no path."""
    home = f"/home/{cfg['remote_user']}"
    p = HOME_RE.sub(lambda _: home, piece.replace("{REMOTE_REPO}", cfg["remote_repo_path"].rstrip("/")))
    p = re.sub(r"^~([a-z_][a-z0-9_-]*)?(?=/|$)", lambda m: f"/home/{m.group(1)}" if m.group(1) else home, p)
    if "/" not in p or p.startswith(("$", "{")):   # another variable's value is unknown here
        return None
    n = posixpath.normpath(re.sub(r"/{2,}", "/", p if p.startswith("/") else f"{cwd}/{p}"))
    return n + "/" if p.endswith("/") and n != "/" else n

def pin_paths(cfg, text, dest, cwd):
    """`text` with each canonically spelled path into snapshot_dirs pointed at `dest`, and [(piece, canonical)] for every
    piece that names a live tree only through another spelling ('//', '/./', '..', $HOME, ${HOME}, ~, or relative to
    the job's `cwd`). Such a piece is never rewritten: the caller refuses it."""
    repo = cfg["remote_repo_path"].rstrip("/")
    live = [repo, cfg["remote_infra_path"].rstrip("/")]
    out, aliases = [], []
    for part in PATH_SEP_RE.split(text):
        canon = None if PATH_SEP_RE.fullmatch(part or " ") else canonical_path(cfg, part, cwd)
        if canon is None or canon == part.replace("{REMOTE_REPO}", repo):
            out.append(point_at_snapshot(cfg, part, dest))
            continue
        if _under(canon.rstrip("/") or "/", live):
            aliases.append((part, canon))
        out.append(part)
    return "".join(out), aliases

def refuse_live(cfg, what, text, aliases):
    """Exit live_tree_reference if `text` still reads a live tree, as written or through `aliases` (pin_paths)."""
    left = live_refs(cfg, text)
    if left or aliases:
        via = "".join(f"\n  {a!r} is another spelling of the live {c}; only a canonical path into snapshot_dirs is "
                      "pointed at the batch's copy" for a, c in aliases)
        sys.exit(f"error_id=live_tree_reference — {what} still reads {left or 'a live tree'}, which a later sync would "
                 f"change under its queued jobs; a batch freezes only its binary and {frozen_scope(cfg)}.{via}\n  {text}")

def live_refs(cfg, text):
    """The live trees `text` still reads: {REMOTE_REPO} anywhere; remote_repo_path or remote_infra_path as a whole path."""
    return (["{REMOTE_REPO}"] if "{REMOTE_REPO}" in text else []) + [
        p for p in (cfg["remote_repo_path"].rstrip("/"), cfg["remote_infra_path"].rstrip("/")) if _path_re(p).search(text)]

def frozen_scope(cfg):
    dirs = cfg["snapshot_dirs"]
    return (", ".join(f"<remote_repo_path>/{d}/" for d in dirs) + " (snapshot_dirs)" if dirs
            else "no directory (snapshot_dirs is empty)")

def snapshot_knobs(cfg, name, knobs, assets):
    """Point one wrapper-route experiment at its batch's snapshot; refuse one that still reads a live tree. Its jobs are
    submitted, and so start, in `assets`."""
    out, aliases = pin_paths(cfg, knobs, assets, assets)
    refuse_live(cfg, f"experiment {name!r}", out, aliases)
    return out

def snapshot_plan(cfg, dest, exists_error, stamp, scripts=False):
    """What a batch freezes into `dest`: the binary as bin/<exe>.<stamp>, each of snapshot_dirs, and (jobfile
    route) remote_infra_path's scripts/. `stamp` also marks dest as this attempt's."""
    repo = cfg["remote_repo_path"].rstrip("/")
    items = [{"src": f"{repo}/{d}", "dst": d, "required": False} for d in cfg["snapshot_dirs"]]
    if scripts:
        items.append({"src": f"{cfg['remote_infra_path'].rstrip('/')}/scripts", "dst": "scripts", "required": True})
    return {"dest": dest, "binary": f"{repo}/{cfg['binary']}", "exe": f"bin/{os.path.basename(cfg['binary'])}.{stamp}",
            "token": stamp, "exists_error": exists_error, "items": items}

def _subtree_digest(manifest, prefix):
    """verify.py's digest of a copy of the manifest tree's <prefix>/ ('' for all of it)."""
    rows = []
    with open(manifest, encoding="utf-8", errors="surrogateescape") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            kind, mode, p = line.rstrip("\n").split("\t", 2)
            if prefix:
                if not p.startswith(prefix + "/"):
                    continue
                p = p[len(prefix) + 1:]
            rows.append((p, f"{kind}\t{mode}\t{p}"))
    return hashlib.sha256("\n".join(r for _, r in sorted(rows)).encode("utf-8", "surrogateescape")).hexdigest()

def snapshot_bundle(cfg, plan, entries, local_dir):
    """Add snapshot.sh and its item list to a sync bundle. An item inside a tree this sync mirrors is re-verified
    against that tree's manifest; returns {dst: expected digest, or None when no manifest covers it}."""
    root, q = cfg["remote_project_root"].rstrip("/"), shlex.quote
    with open(os.path.join(local_dir, "snapshot.sh"), "w") as f:
        f.write(SNAPSHOT_SH.replace("@DEST@", q(plan["dest"])).replace("@BINARY@", q(plan["binary"]))
                .replace("@EXE@", q(plan["exe"])).replace("@TOKEN@", q(plan["token"])))
    rows, expect = [], {}
    for it in plan["items"]:
        e = next((e for e in entries if _under(it["src"], [f"{root}/{e['remote']}"])), None)
        prefix = it["src"][len(f"{root}/{e['remote']}"):].strip("/") if e else ""
        if e and not _under(prefix, e["protect"]):
            expect[it["dst"]] = _subtree_digest(os.path.join(local_dir, e["name"] + ".manifest"), prefix)
            row = [f"{e['name']}.manifest", prefix or "-", ",".join(e["protect"]) or "-"]
        else:
            expect[it["dst"]], row = None, ["-", "-", "-"]
        rows.append("\t".join([it["src"], it["dst"], "1" if it["required"] else "0"] + row) + "\n")
    with open(os.path.join(local_dir, "snapshot.tsv"), "w") as f:
        f.write("".join(rows))
    return expect

def check_snapshot(cfg, plan, expect, out, err, rec):
    """Read snapshot.sh's markers. Refuses a snapshot whose binary is not the one this run built (or found)
    under the lock, or whose copied trees differ from the synced manifests. Returns the ledger record."""
    tail, dest = f"\n{out[-1500:]}\n{err[-500:]}", plan["dest"]
    if re.search(r"^SNAPSHOT_NO_BINARY ", out, re.M):
        sys.exit(f"error_id=build_failed — {plan['binary']} is missing or not executable, so there is no binary to "
                 f"freeze into {dest}. Nothing was queued." + tail)
    kept = f"{dest} stays on the head node as evidence and its label is spent. Nothing was queued."
    bad = re.search(r"^SYNC_VERIFY_FAIL (snapshot:.*)$", out, re.M)
    if bad:
        sys.exit(f"error_id=snapshot_mismatch — the batch's copy differs from the tree this run synced ({bad.group(1)}): "
                 f"something wrote to it without the sync lock. {kept}" + tail)
    failed = re.search(r"^SNAPSHOT_FAILED (.*)$", out, re.M)
    mb = re.search(r"^SNAPSHOT_BIN ([0-9a-f]{64}) ([0-9a-f]{64})$", out, re.M)
    if failed or not mb or not re.search(r"^SNAPSHOT_OK ", out, re.M):
        sys.exit(f"error_id=snapshot_failed — could not freeze the batch into {dest}"
                 + (f" ({failed.group(1)})" if failed else "") + f". {kept}" + tail)
    live, snap, built = mb.group(1), mb.group(2), rec.get("binary_sha256")
    if snap != live or (built and snap != built):
        sys.exit(f"error_id=snapshot_mismatch — the batch's binary has sha256 {snap}, but this run "
                 f"{'built' if rec.get('build') == 'built' else 'found'} {built or live}: something replaced it without "
                 f"the sync lock. {kept}" + tail)
    got = dict(re.findall(r"^SYNC_VERIFY_OK snapshot:(\S+) \d+ ([0-9a-f]{64})$", out, re.M))
    skipped = re.findall(r"^SNAPSHOT_SKIPPED (\S+)$", out, re.M)
    copied = [it["dst"] for it in plan["items"] if it["dst"] not in skipped]
    wrong = [d for d in copied if expect.get(d) and got.get(d) != expect[d]]
    if wrong:
        sys.exit(f"error_id=snapshot_mismatch — the batch's copy of {wrong} does not match the synced manifest. {kept}" + tail)
    unverified = [d for d in copied if not expect.get(d)]
    snap_rec = {"dir": dest, "binary": f"{dest}/{plan['exe']}", "binary_sha256": snap, "snapshot_dirs": list(cfg["snapshot_dirs"]),
                "copied": copied, "skipped": skipped, "verified": rec.get("build") == "built" and not unverified}
    if not snap_rec["verified"]:
        snap_rec["note"] = ("no sync ran (--no-sync), so there were no digests to compare" if not rec["entries"] else
                            f"no sync manifest covers {unverified}; they were copied as the head node had them")
    for d in skipped:
        print(f"[snapshot] WARNING: <remote_repo_path>/{d} does not exist on the head node, so the batch has no {d}/ "
              "and any path an experiment has into it will not resolve.")
    print(f"[snapshot] {dest}: {plan['exe']} (sha256 {snap[:16]}), {', '.join(copied) or 'no dirs'} — "
          + ("identical to what this run synced and built" if snap_rec["verified"] else f"NOT verified: {snap_rec['note']}"))
    return snap_rec

def note_attempt(repo, cfg, batch, rec):
    """Record locally that an attempt froze its batch: from here on it may queue jobs, so its label stays spent
    even if no ledger follows (batch_layout refuses it), and attempt.json holds what `status` needs to recover the
    ledger from the attempt's own S3 record. Returns (run dir, attempt record)."""
    os.makedirs(runs_dir(repo, cfg), exist_ok=True)
    d = os.path.join(runs_dir(repo, cfg), batch)
    try:
        os.mkdir(d)
    except FileExistsError:
        sys.exit(f"error_id=batch_exists — {d} appeared while this attempt froze {rec['snapshot']['dir']}: another submit "
                 "of this label recorded its attempt first. Nothing was queued from this attempt, whose batch dir stays "
                 "on the head node; pick a new --label.")
    att = dict(rec, batch=batch, frozen_at=time.time())
    write_json(os.path.join(d, "attempt.json"), att)
    return d, att

def _input_yaml(path, text):
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as e:
        sys.exit(f"error_id=bad_input — {path} is not valid YAML (indent with spaces, not tabs): {e}")

def _entry_names(path, entries, what, ok):
    """Names from a YAML list of `- <name>: <value>` entries; `ok` checks each value."""
    if not isinstance(entries, list):
        sys.exit(f"error_id=bad_input — {path}: expected a list of `- <name>: ...` {what} entries.")
    names = []
    for e in entries:
        if not isinstance(e, dict) or not e:
            sys.exit(f"error_id=bad_input — {path}: {e!r} is not a `<name>: ...` {what} entry.")
        for n, v in e.items():
            if not ok(v):
                sys.exit(f"error_id=bad_input — {path}: {what} {n!r} is malformed: {v!r}")
            names.append(str(n))
    return names

def expand_exp(path, data):
    """One exp file's experiments with its own `definitions:` expanded exactly as create_jobfile does
    (create_experiments + replace_variables): this file's definitions only, a later duplicate winning, and one
    pass per experiment over the $(VAR)s its own text names -- so a definition's $(VAR) expands only when the
    experiment also names that variable, later. What create_jobfile would reject, or leave as a literal $(...)
    for a job's shell to run, is refused here."""
    defs = data.get("definitions", [])
    if not isinstance(defs, list) or not all(isinstance(d, dict) and d for d in defs):
        sys.exit(f"error_id=bad_input — {path}: `definitions:` must be a list of `- <NAME>: <value>` entries.")
    definitions = {list(d.keys())[0]: list(d.values())[0] for d in defs}
    out = []
    for exp in data.get("experiments", []):
        for name, params in exp.items():
            for var in VAR_RE.findall(params):
                if var not in definitions:
                    sys.exit(f"error_id=unresolved_variable — {path}: experiment {name!r} uses $({var}), which that "
                             "file does not define; create_jobfile expands each exp file with its own definitions "
                             "only (CJ_UNDEFINED_VAR). cluster_run's $(SIM_HOME_IN_CLUSTER) is not defined here.")
                if not isinstance(definitions[var], str):
                    sys.exit(f"error_id=bad_input — {path}: definition {var!r} is {definitions[var]!r}; quote it, "
                             "create_jobfile substitutes strings only.")
                params = params.replace(f"$({var})", definitions[var])
            left = VAR_RE.findall(params)
            if left:
                sys.exit(f"error_id=unresolved_variable — {path}: experiment {name!r} still holds $({left[0]}) after "
                         "create_jobfile's one expansion pass (a definition that uses another stays literal, and a "
                         "job's shell would run it). Inline the inner value.")
            out.append((name, params))
    return out

def resolve_exp(cfg, path, data, run):
    """The <name>.resolved.yml create_jobfile runs: every experiment expanded (expand_exp) with its canonically spelled
    paths into snapshot_dirs pointed at <run>, and no definitions left. An experiment that still reads a live tree once
    expanded -- however a definition split or spelled the path -- is refused: a later sync changes it under queued
    jobs. Its jobs start in <run>, so a relative path is taken from there."""
    exps = []
    for name, params in expand_exp(path, data):
        p, aliases = pin_paths(cfg, params, run, run)
        refuse_live(cfg, f"{path}: experiment {name!r}, once expanded and with its snapshot paths on {run},", p, aliases)
        exps.append({name: p})
    body = yaml.safe_dump({"experiments": exps}, sort_keys=False, width=1 << 20)   # non-ASCII escaped: U+0085 survives
    if yaml.safe_load(body) != {"experiments": exps}:
        sys.exit(f"error_id=bad_input — {path}: its experiments do not load back unchanged from YAML, so create_jobfile "
                 "would run other params than these; remove the unusual characters.")
    return (f"# aws-launch: {os.path.basename(path)} as this batch runs it -- $(VAR)s expanded as create_jobfile does,\n"
            "# paths into snapshot_dirs on the batch's own copy.\n" + body)

def jobfile_inputs(cfg, args, run):
    """Check the tlist/exp/mfile files and resolve each exp against the batch's config snapshot, locally,
    so a bad input costs no wake. Returns (files to stage as [(name, text)], staged names by kind, pairs)."""
    groups = {"tlist": args.tlist, "exp": args.exp, "mfile": args.mfile}
    names = [os.path.basename(p) for g in groups.values() for p in g]
    names += [os.path.splitext(os.path.basename(p))[0] + ".resolved.yml" for p in args.exp]
    bad = [n for n in names if not INPUT_NAME_RE.match(n)]
    if bad:
        sys.exit(f"error_id=bad_input_name — {bad}: staged input names reach remote shells, so use letters, "
                 "digits and ._+- only.")
    clash = sorted(n for n, c in collections.Counter(names).items() if c > 1)
    if clash:
        sys.exit(f"error_id=input_name_collision — inputs are staged into one directory by basename, each exp "
                 f"beside its <name>.resolved.yml, so these collide: {clash}")
    files, found = [], {"trace": [], "experiment": []}
    staged = {"tlist": [], "exp": [], "exp_authored": [], "mfile": []}
    for kind, paths in groups.items():
        for p in paths:
            if not os.path.isfile(p):
                sys.exit(f"error_id=input_not_found — --{kind} {p} does not exist.")
            with open(p) as f:
                text = f.read()
            data, name = _input_yaml(p, text), os.path.basename(p)
            files.append((name, text))
            if kind == "tlist":
                if not isinstance(data, dict):
                    sys.exit(f"error_id=bad_input — {p}: a trace list maps each suite to a list of traces.")
                for entries in data.values():
                    found["trace"] += _entry_names(p, entries, "trace",
                                                   lambda v: isinstance(v, dict) and bool(v.get("path")))
                staged["tlist"].append(name)
            elif kind == "exp":
                exps = data.get("experiments") if isinstance(data, dict) else None
                found["experiment"] += _entry_names(p, exps, "experiment", lambda v: isinstance(v, str))
                resolved = os.path.splitext(name)[0] + ".resolved.yml"
                files.append((resolved, resolve_exp(cfg, p, data, run)))
                staged["exp"].append(resolved); staged["exp_authored"].append(name)
            else:
                _entry_names(p, data, "metric", lambda v: v is not None)
                staged["mfile"].append(name)
    for what, got in found.items():
        dup = sorted(n for n, c in collections.Counter(got).items() if c > 1)
        if dup:
            sys.exit(f"error_id=duplicate_name — {what} name(s) {dup} appear more than once across the inputs; "
                     "create_jobfile refuses them.")
    pairs = len(found["trace"]) * len(found["experiment"])
    if not pairs:
        sys.exit("error_id=empty_spec — the inputs name no (trace, experiment) pair.")
    return files, staged, pairs

JOBFILE_LAUNCH_SH = r"""#!/bin/bash
# aws-launch jobfile submit, head-node side. The run dir already holds this batch's bin/, config/ and
# scripts/, frozen under the sync lock; this stages the inputs beside them and lets create_jobfile
# smoke-test and launch the jobs from there, on the pinned binary. The report returns through S3 -- SSM
# keeps 24,000 characters of stdout -- and its upload is retried: it holds the queued job ids. SSM's
# timeout kill need not reach this script (runuser -c starts a new session), so create_jobfile runs
# under `timeout`, which stops it and every sbatch it started before SSM gives up.
set -uo pipefail
export PATH=/opt/slurm/bin:$PATH
RUN=@RUN@; KEY=@KEY@; R=@REGION@; TOKEN=@TOKEN@
put() {
  local i
  for i in 1 2 3; do
    aws s3 cp "$1" "$2" --region "$R" --no-progress --only-show-errors && return 0
    [ "$i" = 3 ] || sleep $((i * 2))
  done
  return 1
}
cd "$RUN" 2>/dev/null && [ "$(cat .aws-launch-snapshot 2>/dev/null)" = "$TOKEN" ] \
  || { echo "JF_NOT_SNAPSHOT $RUN"; exit 3; }
# only a launch of this very attempt makes inputs/, so if it exists that launch ran or is running
mkdir inputs 2>/dev/null || { if [ -e inputs ]; then echo "JF_ALREADY_LAUNCHED $RUN"; else echo "JF_STAGE_FAILED mkdir"; fi; exit 4; }
aws s3 cp --recursive "$KEY/inputs/" inputs/ --region "$R" --no-progress --only-show-errors \
  || { echo "JF_STAGE_FAILED inputs"; exit 4; }
timeout -k 60 @BOUND@ python3 scripts/create_jobfile.py --exe @EXE@ --no-snapshot-exe --wrapper "$RUN/scripts/run_champsim.py" \
  --trace-cache-dir @CACHE@ --slurm-part @PARTITION@ --ncores @NCORES@ --extra @EXTRA@ \
  --exp @EXPS@ --tlist @TLISTS@ -o jobfile.sh --smoke-test-auto-launch --smoke-test-idx @IDX@ \
  --smoke-warmup 1000000 --smoke-sim 1000000 --report-json report.json > create_jobfile.log 2>&1
rc=$?
if [ "$rc" = 124 ] || [ "$rc" = 137 ]; then echo "JF_LAUNCH_TIMEOUT @BOUND@"; fi
put create_jobfile.log "$KEY/create_jobfile.log" || echo "JF_LOG_UPLOAD_FAILED"
if [ -f report.json ]; then
  put report.json "$KEY/report.json" || echo "JF_REPORT_UPLOAD_FAILED $RUN/report.json"
else
  echo "JF_NO_REPORT"
fi
echo "JF_DONE rc=$rc"
"""

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
    cfg["project"] = args.project
    cfg["remote_project_root"] = (args.project_root
                                  or f"/home/{cfg['remote_user']}/{args.project}")
    cfg["remote_repo_path"] = f"{cfg['remote_project_root']}/Hermes"
    cfg["remote_infra_path"] = f"{cfg['remote_project_root']}/champsim-infra"
    cfg.setdefault("trace_cache_dir", "/scratch/trace_cache")
    cfg.setdefault("snapshot_dirs", ["config"])
    cfg.setdefault("launch_timeout_s", LAUNCH_TIMEOUT_S)
    cfg["default_knobs"] = cfg["default_knobs"].replace("{REMOTE_REPO}", cfg["remote_repo_path"])
    cfg["sync"] = default_sync(repo, cfg)
    validate_cfg(cfg, p)   # layout first — cheap, and not masked by an AWS error
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
    for e in cfg["sync"]:
        print(f"[configure] sync: {e['local']} -> <remote_project_root>/{e['remote']} (protect "
              f"{e['protect']}). Review it: sync mirrors with --delete.")
    if not cfg["sync"]:
        print("[configure] WARNING: no git work tree holding the build script was found, so `sync` is "
              "empty and `submit` will refuse to run. Add a `sync:` list (see config-template.yml).")

def verb_sync(args):
    repo = args.repo; cfg = load_cfg(repo)
    entries = sync_entries(repo, cfg)          # config errors surface before anything wakes
    head = wake(cfg); print(f"[sync] head={head} awake")
    sync_push(cfg, repo, head, entries, build=args.build)
    if not args.build:
        print("[sync] binary NOT rebuilt: it predates this source until `sync --build` or `submit`.")

def _knobs(cfg, override):
    k = override or cfg["default_knobs"]
    return " ".join(k.split()).replace("{REMOTE_REPO}", cfg["remote_repo_path"])

def resolve_trace(cfg, t):
    """Trace keys passed to the job wrapper are relative to the TRACES BUCKET ROOT.

    A bare filename (no '/') gets `trace_prefix` prepended, so existing trace lists keep
    working; anything already containing a '/' is taken as an explicit key (e.g.
    "version2.1/spark/x.champsim2.zst"). This MUST be applied everywhere a trace is
    resolved -- the smoke gate previously prepended trace_prefix while the submitted
    jobs did not, so the gate passed and then every job died at stage-in with exit 90."""
    t = t.strip().lstrip("/")
    if "/" in t:
        return t
    pref = (cfg.get("trace_prefix") or "").strip("/")
    return f"{pref}/{t}" if pref else t

def boot_prefix(cfg):
    """Per-PROJECT bootstrap prefix. `project` is OWNER-FIRST, e.g. "rbera/hermes-uncore",
    so everything a person owns sits under one top-level prefix and a single IAM statement
    (`<bucket>/<owner>/*`) scopes them. Never share one key across projects/users: a second
    project (or a teammate) uploading its wrapper would silently replace yours."""
    return f"{cfg['s3_results']}/{cfg['project']}/bootstrap"

def results_prefix(cfg):
    """Per-PROJECT results prefix, so `collect` never mixes two projects' output."""
    return f"{cfg['s3_results']}/{cfg['project']}/results"

def render_wrapper(cfg, src, binary):
    """The bundled wrapper is a TEMPLATE: its #SBATCH -o/-e lines cannot use a shell
    variable (Slurm parses them before any shell runs), so the project root is
    substituted here, at upload time, per project. `binary` is the batch's snapshot,
    relative to the project root."""
    txt = (open(src).read()
           .replace("{{PROJECT_ROOT}}", cfg["remote_project_root"])
           .replace("{{PROJECT}}", cfg["project"])
           # The jobs must run the SAME binary the smoke gate tested, and a rebuild must not swap it.
           .replace("{{BINARY}}", binary))
    tf = tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False)
    tf.write(txt); tf.close()
    return tf.name

def verb_submit(args):
    (submit_jobfile if args.tlist else submit_wrapper)(args)

def launch_bound(cfg):
    """Seconds a head-node launch runs before its own `timeout` stops it, with every sbatch it started: SSM's
    executionTimeout kill need not reach a command under `runuser -c` (a new session), so the launch ends first."""
    return cfg["launch_timeout_s"] - LAUNCH_MARGIN_S

def wrapper_record_jobs(text, token):
    """(jobs, "") from a wrapper-route submitted.txt, or (None, why) unless it is this attempt's whole record: its first
    line names the attempt's token, and a closing SUBMIT_COMPLETE line counts every SUBMIT line."""
    lines = text.splitlines()
    if not lines or lines[0] != f"ATTEMPT {token}":
        return None, f"does not open with this attempt's token (ATTEMPT {token})"
    jobs = [{"exp": m.group(1), "trace": m.group(2), "job_id": m.group(3), "tag": f"{m.group(1)}:{m.group(2)}"}
            for m in re.finditer(r"^SUBMIT (\S+) (\S+) (\d+)$", text, re.M)]
    done = re.fullmatch(r"SUBMIT_COMPLETE (\d+)", lines[-1])
    if not done or int(done.group(1)) != len(jobs):
        return None, f"is not closed by a SUBMIT_COMPLETE line counting its {len(jobs)} job line(s)"
    return jobs, ""

def wrapper_ledger(att, jobs, submitted_at):
    return {"batch": att["batch"], "mode": "wrapper", "state": "submitted", "submitted_at": submitted_at,
            "run_assets": att["run_assets"], "n_jobs": len(jobs), "incomplete": len(jobs) != att["n_expected"],
            "sync": att["sync"], "snapshot": att["snapshot"], "jobs": jobs}

def submit_wrapper(args):
    repo = args.repo; cfg = load_cfg(repo)
    batch = batch_layout(cfg, args, repo)
    assets = f"{cfg['remote_project_root'].rstrip('/')}/run-assets/{batch}"
    traces = [t.strip() for t in open(args.traces) if t.strip()]
    spaced = [t for t in traces if re.search(r"\s", t)]
    if spaced:
        sys.exit(f"error_id=bad_input — trace key(s) {spaced[:3]} in {args.traces} hold whitespace, which the job-id "
                 "record (one `SUBMIT <exp> <trace> <jobid>` line per job) cannot carry.")
    exps = {}
    for spec in args.exps.split(";"):
        name, _, knobs = spec.partition("=")
        name = name.strip()
        if not INPUT_NAME_RE.match(name):
            sys.exit(f"error_id=bad_input_name — experiment name {name!r} in --exps: use letters, digits and ._+- "
                     "(starting with a letter or digit). Names reach remote shells and the one-line-per-job record.")
        exps[name] = snapshot_knobs(cfg, name, _knobs(cfg, knobs.strip() or None), assets)
    if not traces or not exps:
        sys.exit("error_id=empty_spec")
    expected = len(traces) * len(exps)
    print(f"[submit] batch={batch} traces={len(traces)} exps={list(exps)} -> {expected} jobs")
    entries = None if args.no_sync else sync_entries(repo, cfg)   # validate before waking
    stamp = attempt_stamp()
    record_key = f"{boot_prefix(cfg)}/{batch}.{stamp}.submitted.txt"   # per attempt: no other attempt's record can pass
    with label_claim(repo, cfg, batch):
        head = wake(cfg); print(f"[submit] head={head} awake")
        if entries is None:
            print("[submit] WARNING: --no-sync — the head node keeps whatever source and binary it already has "
                  "(building one only if none exists), and nothing checks that they match your tree.")
        # --no-sync keeps the old contract: build only when no binary exists at all. Either way the batch is frozen
        # under the sync lock, right after.
        plan = snapshot_plan(cfg, assets, "batch_exists", stamp)
        rec = sync_push(cfg, repo, head, entries or [], build=True if entries is not None else "if-missing", snapshot=plan)
        sync_rec, snap = (rec if entries is not None else None), rec["snapshot"]
        outdir, att = note_attempt(repo, cfg, batch, {"mode": "wrapper", "token": stamp, "run_assets": assets,
                                                      "record": record_key, "n_expected": expected,
                                                      "snapshot": snap, "sync": sync_rec})
    binary = f"run-assets/{batch}/{plan['exe']}"

    # stage job wrapper + trace list to S3. The wrapper names this batch's binary, so its key is per batch.
    wrapper = render_wrapper(cfg, os.path.join(SKILL_DIR, cfg["job_wrapper"]), binary)
    s3_put(cfg, wrapper, f"{boot_prefix(cfg)}/{batch}.champsim-job.sh")
    tl = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
    tl.write("\n".join(resolve_trace(cfg, t) for t in traces) + "\n"); tl.close()
    s3_put(cfg, tl.name, f"{boot_prefix(cfg)}/{batch}.traces.txt")

    # --- MANDATORY head-node gate -------------------------------------------------
    # Runs THIS wrapper -- same binary, same trace resolution, same knobs -- on the
    # head node, which shares the compute nodes' aarch64 ISA. Seconds, no Spot boot.
    # It is the wrapper that matters, not the location: reimplementing the job path is
    # what let a hardcoded binary (exit 127, 900 jobs) and a missing trace prefix
    # (exit 90, 450 jobs) through a green gate. It cannot see compute-node-only
    # problems (their separate IAM profile, the OnNodeConfigured bootstrap script,
    # /scratch provisioning) -- use --spot-smoke for those on an unproven cluster.
    smoke_exp = next(iter(exps))
    smoke_knobs = re.sub(r"--warmup_instructions=\d+", "--warmup_instructions=1000000",
                  re.sub(r"--simulation_instructions=\d+", "--simulation_instructions=1000000",
                         exps[smoke_exp]))
    PRH, RH = cfg["remote_project_root"], cfg["region"]
    hs = f"/tmp/hsmoke.{batch}"
    htag = f"hsmoke_{smoke_exp}.{stamp}"   # per attempt: an earlier attempt's result cannot pass this gate
    # Knobs go in a FILE, never inline. shlex.quote wraps in single quotes, which
    # closes the outer `runuser -c '...'` -- the same nested-quoting trap the real
    # submitter avoids by shipping knobs in a file ("no SSM quoting to get wrong").
    hr = tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False)
    hr.write("#!/bin/bash\nset -uo pipefail\n"
             f"mkdir -p {hs}\n"
             f"aws s3 cp {boot_prefix(cfg)}/{batch}.champsim-job.sh {assets}/champsim-job.sh "
             f"--region {RH} --no-progress\n"
             f"chmod +x {assets}/champsim-job.sh\n"
             f"export SCRATCH={hs} SLURM_JOB_ID=hsmoke PROJECT_ROOT={PRH}\n"
             f"bash {assets}/champsim-job.sh \\\n"
             f"  {resolve_trace(cfg, traces[0])} \\\n"
             f"  {htag} \\\n"
             f"  {shlex.quote(smoke_knobs)} \\\n"
             f"  {cfg['project']}/results 2>&1 | tail -6\n"
             f"rm -rf {hs}\n")
    hr.close()
    s3_put(cfg, hr.name, f"{boot_prefix(cfg)}/{batch}.hsmoke.sh")
    os.unlink(hr.name)
    print("[submit] head-node smoke: running the real wrapper...")
    st, out, err = ssm_run(cfg, head, [
        f"runuser -l {cfg['remote_user']} -c "
        f"'aws s3 cp {boot_prefix(cfg)}/{batch}.hsmoke.sh /tmp/{batch}.hsmoke.sh "
        f"--region {RH} --no-progress && bash /tmp/{batch}.hsmoke.sh; "
        f"rm -f /tmp/{batch}.hsmoke.sh'"],
        timeout=1200)
    # Read the S3 artifact, not local scratch: the wrapper does `rm -f "$OUT"` once the
    # upload succeeds, so the file is gone by the time we look. Checking S3 also proves
    # the upload leg -- the last hop between a finished job and a usable number.
    hkey = f"{results_prefix(cfg)}/{htag}/"
    hobj = next((l.split()[-1] for l in aws(cfg, ["s3", "ls", hkey]).splitlines()
                 if l.strip().endswith(".txt")), None)
    if not hobj:
        sys.exit(f"error_id=head_smoke_no_result — the wrapper left nothing under {hkey}. "
                 f"The array was NOT submitted.\n{out[-1000:]}\n{err[-300:]}")
    with tempfile.NamedTemporaryFile("r+", suffix=".txt", delete=False) as hf:
        pass
    s3_get(cfg, hkey + hobj, hf.name)
    body = open(hf.name, errors="replace").read()
    os.unlink(hf.name)
    if "Finished CPU 0" not in body or not re.search(r"^champsim_exit_code 0$", body, re.M):
        sys.exit(f"error_id=head_smoke_failed — the wrapper did not produce a clean result "
                 f"on the head node. The array was NOT submitted.\nresult tail:\n{body[-900:]}")
    hipc = re.search(r"^Core_0_cumulative_IPC (\S+)$", body, re.M)
    print(f"[submit] head-node smoke OK — IPC={hipc.group(1) if hipc else '?'}")
    aws(cfg, ["s3", "rm", hkey, "--recursive"], check=False)

    if not getattr(args, "spot_smoke", False):
        print("[submit] spot-node smoke: disabled (default; --spot-smoke enables it). "
              "The head-node gate above ran the real wrapper; a spot gate additionally "
              "covers compute-only surfaces (their IAM profile, the OnNodeConfigured "
              "bootstrap script, /scratch) and is worth it on an unproven cluster.")
    else:
        print("[submit] binary present; smoke-gating ONE real job through the wrapper...")

        # The gate runs the SAME wrapper, submitter and binary the array will use -- only
        # the instruction counts differ (cf. cluster-run, whose gate calls the very same
        # wrap_with_orchestrator() as its job loop). A gate that reimplements the job path
        # certifies a path nothing will take: this one previously staged the trace itself
        # and invoked cfg["binary"] directly, so it passed while every submitted job died
        # -- once on a trace-prefix mismatch (exit 90), once on a hardcoded binary the
        # project did not have (exit 127). Both were invisible until the results were read.
        smoke_exp = next(iter(exps))
        smoke_knobs = re.sub(r"--warmup_instructions=\d+", "--warmup_instructions=1000000",
                      re.sub(r"--simulation_instructions=\d+", "--simulation_instructions=1000000",
                             exps[smoke_exp]))
        stag = f"smoke_{smoke_exp}.{stamp}"   # the gate job's name and result key, per attempt
        sk_tl = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        sk_tl.write(resolve_trace(cfg, traces[0]) + "\n"); sk_tl.close()
        sk_ef = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        sk_ef.write(f"{stag}\t{smoke_knobs}\n"); sk_ef.close()
        s3_put(cfg, sk_tl.name, f"{boot_prefix(cfg)}/{batch}.smoke.traces.txt")
        s3_put(cfg, sk_ef.name, f"{boot_prefix(cfg)}/{batch}.smoke.exps.txt")
        s3_put(cfg, os.path.join(SKILL_DIR, "scripts", "_submit_remote.sh"),
               f"{boot_prefix(cfg)}/_submit_remote.sh")
        BOOT0, R0, PR0 = boot_prefix(cfg), cfg["region"], cfg["remote_project_root"]
        st, out, err = ssm_run(cfg, head, [
            f"runuser -l {cfg['remote_user']} -c 'set -e; cd {assets}; "
            f"aws s3 cp {BOOT0}/_submit_remote.sh sb.sh --region {R0} --no-progress; "
            f"aws s3 cp {BOOT0}/{batch}.smoke.traces.txt st.txt --region {R0} --no-progress; "
            f"aws s3 cp {BOOT0}/{batch}.smoke.exps.txt se.txt --region {R0} --no-progress; "
            f"bash sb.sh {BOOT0} {PR0} st.txt se.txt {cfg['partition']} "
            f"{cfg['ncores_per_job']} {cfg.get('walltime','24:00:00')} {R0} {batch}.{stamp}.smoke {batch} {stamp}'"],
            timeout=600)
        m = re.search(r"^SUBMIT \S+ \S+ (\d+)$", out, re.M)
        if not m:
            rs = "\n".join("  " + x.group(1) for x in re.finditer(r"^SUBMIT_FAIL_REASON (.+)$", out, re.M))
            if re.search(r"^(SUBMIT_COMPLETE|SUBMIT_FAILED|SUBMIT_RECORD_UPLOAD_FAILED) ", out, re.M):
                sys.exit(f"error_id=smoke_submit_failed — the gate job could not be queued; nothing was queued.\n{rs}\n"
                         f"{out[-400:]}\n{err[-200:]}")
            sys.exit(f"error_id=smoke_submit_failed — the gate submit ended {st} without showing whether its sbatch ran, "
                     f"so the gate job MAY be queued: find a job named {stag}-<trace> in squeue and scancel it. The array "
                     f"was NOT submitted.\n{out[-400:]}\n{err[-200:]}")
        sjid = m.group(1)
        att["smoke_job_id"] = sjid
        write_json(os.path.join(outdir, "attempt.json"), att)
        print(f"[submit] smoke job {sjid} queued; waiting (a Spot node may need to boot)...")
        deadline = time.time() + 1800
        while time.time() < deadline:
            time.sleep(20)
            st, qo, _ = ssm_run(cfg, head, [
                f"export PATH=/opt/slurm/bin:$PATH; echo QS; squeue -h -j {sjid} -o '%T' 2>/dev/null; echo QE"], timeout=120)
            if "QS" in qo and "QE" in qo and not re.search(r"^(PENDING|RUNNING|CONFIGURING|COMPLETING)$", qo, re.M):
                break
        else:
            sys.exit(f"error_id=smoke_timeout — gate job {sjid} did not finish within 30 min and MAY still be queued: "
                     f"scancel {sjid}. The array was NOT submitted.")
        # Check the RESULT OBJECT the analysis will consume, not the slurm .out: the
        # wrapper redirects the simulator's stdout into $OUT and uploads that, so the
        # slurm log holds only the wrapper's own echoes. Reading the S3 artifact also
        # proves the upload leg works, which is the last thing between a finished job
        # and a usable number.
        skey = f"{results_prefix(cfg)}/{stag}/"
        listing = aws(cfg, ["s3", "ls", skey])
        obj = next((l.split()[-1] for l in listing.splitlines() if l.strip().endswith(".txt")), None)
        if not obj:
            sys.exit(f"error_id=smoke_no_result — gate job {sjid} left nothing under {skey}. "
                     f"The array was NOT submitted.")
        with tempfile.NamedTemporaryFile("r+", suffix=".txt", delete=False) as rf:
            pass
        s3_get(cfg, skey + obj, rf.name)
        body = open(rf.name, errors="replace").read()
        os.unlink(rf.name)
        ok_rc = re.search(r"^champsim_exit_code 0$", body, re.M)
        ok_fin = "Finished CPU 0" in body
        if not (ok_rc and ok_fin):
            sys.exit(f"error_id=smoke_failed — gate job {sjid} did not produce a clean result "
                     f"(exit_code_0={bool(ok_rc)} finished={ok_fin}). The array was NOT submitted.\n"
                     f"result tail:\n{body[-900:]}")
        ipc = re.search(r"^Core_0_cumulative_IPC (\S+)$", body, re.M)
        print(f"[submit] smoke OK — real job finished, IPC={ipc.group(1) if ipc else '?'}")
        aws(cfg, ["s3", "rm", skey + obj])   # keep the gate's output out of collect
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
    wall, bound = cfg.get("walltime", "24:00:00"), launch_bound(cfg)
    # everything lands in this batch's run-assets dir, never $HOME; sb.sh stops itself before SSM's timeout
    runuser_cmd = (
        f"runuser -l {cfg['remote_user']} -c 'set -e; cd {assets}; "
        f"aws s3 cp {BOOT}/_submit_remote.sh sb.sh --region {R} --no-progress; "
        f"aws s3 cp {BOOT}/{batch}.traces.txt traces.txt --region {R} --no-progress; "
        f"aws s3 cp {BOOT}/{batch}.exps.txt exps.txt --region {R} --no-progress; set +e; "
        f"timeout -k 60 {bound} bash sb.sh {BOOT} {PR} traces.txt exps.txt {cfg['partition']} "
        f"{cfg['ncores_per_job']} {wall} {R} {batch}.{stamp} {batch} {stamp}; rc=$?; "
        f"if [ $rc -eq 124 ] || [ $rc -eq 137 ]; then echo SUBMIT_TIMEOUT {bound}; fi; exit $rc'")
    st, out, err = ssm_run(cfg, head, [runuser_cmd], timeout=cfg["launch_timeout_s"])

    # Job ids come from S3, NOT from `out`: SSM truncates StandardOutputContent at
    # 24,000 chars, which silently drops the tail at roughly 340 jobs. Parsing stdout
    # once wrote a 335-entry ledger for a 450-job batch and reported success, so
    # `collect` would have skipped 115 finished jobs with nothing anywhere to show it.
    # So there is no stdout fallback: without this attempt's whole record, the error says jobs MAY be queued.
    local_record = os.path.join(outdir, "submitted.txt")   # kept beside the ledger
    try:
        s3_get_retry(cfg, record_key, local_record)
        with open(local_record) as f:
            jobs, missing = wrapper_record_jobs(f.read(), stamp)
    except Exception as e:
        jobs, missing = None, f"never reached S3 ({e})"
    # The remote side summarises sbatch rejections by DISTINCT reason, so a
    # wholly-rejected batch reports its one cause instead of N identical lines.
    reasons = "\n".join("  " + m.group(1)
                        for m in re.finditer(r"^SUBMIT_FAIL_REASON (.+)$", out, re.M))
    why = f"\nsbatch rejections:\n{reasons}" if reasons else ""
    if jobs is None:
        bounded = re.search(r"^SUBMIT_TIMEOUT ", out, re.M)
        sys.exit(f"error_id=submit_no_record — the submitted-job list {record_key} {missing}; the submit command ended {st}"
                 + (f", stopped by its own bound ({bound} s, launch_timeout_s - {LAUNCH_MARGIN_S})" if bounded else "")
                 + f". Jobs MAY be queued, and no ledger was written: check squeue and {assets}/submitted.txt on the head "
                 f"node before resubmitting; `status --batch {batch}` writes the ledger if this attempt's record reaches S3."
                 f"{why}\nstdout tail:\n{out[-400:]}\n{err[-200:]}")
    if not jobs:
        sys.exit(f"error_id=submit_failed — no jobs were queued.{why}\n"
                 f"stdout tail:\n{out[-400:]}\n{err[-200:]}")
    led = wrapper_ledger(att, jobs, time.time())
    write_json(ledger_path(repo, cfg, batch), led)   # the recorded ids are kept even when short
    # A short ledger silently shrinks what collect reads, so say so loudly.
    if len(jobs) != expected:
        sys.exit(f"error_id=ledger_incomplete — recorded {len(jobs)} job ids but "
                 f"{expected} were expected ({len(traces)} traces x {len(exps)} exps). The ledger keeps those "
                 f"{len(jobs)}; check squeue before resubmitting the rest."
                 f"{why}\nstdout tail:\n{out[-400:]}\n{err[-200:]}")
    print(f"[submit] OK — batch {batch}: {len(jobs)} jobs queued.")

def jobfile_result(report):
    """(queued any, ok, jobs, sbatch failures) from create_jobfile's report. As cluster_run: a smoke failure or any
    pre-submit error queued nothing; a partial submit (CJ_SUBMIT_FAILED with ids) queued real jobs."""
    ok = report.get("status") == "ok"
    jobs = [{"tag": j["tag"], "job_id": str(j["job_id"])} for j in report.get("jobs") or [] if j.get("job_id")]
    return ok or (report.get("error_id") == "CJ_SUBMIT_FAILED" and bool(jobs)), ok, jobs, report.get("submit_failures") or []

def jobfile_ledger(att, report, ok, jobs, submitted_at):
    return {"batch": att["batch"], "mode": "jobfile", "state": "submitted", "submitted_at": submitted_at,
            "remote_run_dir": att["remote_run_dir"], "s3": att["s3"], "partial": not ok, "n_jobs": len(jobs),
            "num_pairs": report.get("num_pairs"), "exe": report.get("exe"), "exe_original": report.get("exe_original"),
            "inputs": att["inputs"], "local_inputs": att["local_inputs"],
            "smoke": {k: v for k, v in (report.get("smoke") or {}).items() if k != "output_tail"},
            "snapshot": att["snapshot"], "sync": att["sync"], "jobs": jobs}

def submit_jobfile(args):
    """create_jobfile route: the batch's run dir, <remote_project_root>/results/<batch>/, holds its binary
    snapshot, config/, scripts/, inputs/ and every job's .out/.err."""
    repo = args.repo; cfg = load_cfg(repo)
    batch = batch_layout(cfg, args, repo)
    require_safe(cfg, ("trace_cache_dir",), "bad_config")
    root, infra = cfg["remote_project_root"].rstrip("/"), cfg["remote_infra_path"].rstrip("/")
    run = f"{root}/results/{batch}"
    files, staged, pairs = jobfile_inputs(cfg, args, run)
    idx = args.smoke_idx or 0
    if not 0 <= idx < pairs:
        sys.exit(f"error_id=bad_smoke_idx — --smoke-idx {idx} is outside the {pairs} (trace x exp) pairs.")
    stamp = attempt_stamp()
    plan = snapshot_plan(cfg, run, "run_dir_exists", stamp, scripts=True)
    key = f"{boot_prefix(cfg)}/jobfile/{batch}/{stamp}"   # per attempt: no other attempt's report can pass for this one's
    q = shlex.quote
    launch = (JOBFILE_LAUNCH_SH
              .replace("@RUN@", q(run)).replace("@KEY@", q(key)).replace("@REGION@", q(cfg["region"]))
              .replace("@TOKEN@", q(stamp)).replace("@EXE@", q(f"{run}/{plan['exe']}"))
              .replace("@CACHE@", q(cfg["trace_cache_dir"]))
              .replace("@PARTITION@", q(str(cfg["partition"]))).replace("@NCORES@", q(str(cfg["ncores_per_job"])))
              .replace("@EXTRA@", q(f"--requeue --time={cfg.get('walltime', '24:00:00')} "
                                    f"--export=ALL,AWS_DEFAULT_REGION={cfg['region']}"))
              .replace("@EXPS@", " ".join(q(f"inputs/{n}") for n in staged["exp"]))
              .replace("@TLISTS@", " ".join(q(f"inputs/{n}") for n in staged["tlist"]))
              .replace("@IDX@", str(idx)).replace("@BOUND@", str(launch_bound(cfg))))
    entries = None if args.no_sync else sync_entries(repo, cfg)   # validate before waking
    if entries is not None and not any(f"{root}/{e['remote']}" == infra for e in entries):
        print(f"[submit] WARNING: no `sync:` entry mirrors remote_infra_path ({infra}); the batch copies "
              "whatever scripts/ the head node already has there.")
    print(f"[submit] batch={batch} (jobfile route): {pairs} (trace x exp) jobs, run dir {run}")
    with label_claim(repo, cfg, batch):
        head = wake(cfg); print(f"[submit] head={head} awake")
        if entries is None:
            print("[submit] WARNING: --no-sync — the batch freezes whatever binary, config and scripts/ the head node "
                  "already has, and nothing checks that they match your tree.")
        rec = sync_push(cfg, repo, head, entries or [], build=entries is not None, snapshot=plan)
        sync_rec, snap = (rec if entries is not None else None), rec["snapshot"]
        outdir, att = note_attempt(repo, cfg, batch, {
            "mode": "jobfile", "token": stamp, "remote_run_dir": run, "s3": key, "inputs": staged, "pairs": pairs,
            "local_inputs": {k: [os.path.abspath(p) for p in getattr(args, k)] for k in ("tlist", "exp", "mfile")},
            "snapshot": snap, "sync": sync_rec})
    os.makedirs(os.path.join(outdir, "inputs"))
    for name, text in files:
        with open(os.path.join(outdir, "inputs", name), "w") as f:
            f.write(text)
    with open(os.path.join(outdir, "launch.sh"), "w") as f:
        f.write(launch)
    aws(cfg, ["s3", "cp", "--recursive", os.path.join(outdir, "inputs") + "/", f"{key}/inputs/",
              "--no-progress", "--only-show-errors"])
    s3_put(cfg, os.path.join(outdir, "launch.sh"), f"{key}/launch.sh")
    U, R = cfg["remote_user"], cfg["region"]
    print("[submit] staging the inputs and running create_jobfile (smoke gate, then sbatch) on the head node...")
    st, out, err = ssm_run(cfg, head, [
        f"{{ mkdir -p {HEAD_SCRATCH} && chown {U}: {HEAD_SCRATCH}; }} || {{ echo JF_SCRATCH_FAILED; exit 9; }}; "
        f"runuser -l {U} -c 'F=$(mktemp /tmp/aws-launch-jobfile.XXXXXX) && "
        f"aws s3 cp {key}/launch.sh $F --region {R} --no-progress --only-show-errors && bash $F; "
        f"rc=$?; rm -f $F; exit $rc'"], timeout=cfg["launch_timeout_s"])
    # Only these markers prove create_jobfile never ran; everything else is settled by the report.
    nothing = f"Nothing was queued; {run} keeps the batch's snapshot and its label is spent."
    if "JF_SCRATCH_FAILED" in out:
        sys.exit(f"error_id=scratch_failed — could not give {U} a {HEAD_SCRATCH} on the head node. {nothing}\n{err[-400:]}")
    if re.search(r"^JF_NOT_SNAPSHOT ", out, re.M):
        sys.exit(f"error_id=snapshot_failed — {run} does not hold this attempt's snapshot, so nothing was staged "
                 f"into it. {nothing}\n{out[-1000:]}\n{err[-500:]}")
    m = re.search(r"^JF_STAGE_FAILED (\S+)", out, re.M)
    if m:
        sys.exit(f"error_id=stage_failed — could not stage the inputs into {run} ({m.group(1)}). {nothing}"
                 f"\n{out[-1000:]}\n{err[-500:]}")
    log, rep = os.path.join(outdir, "create_jobfile.log"), os.path.join(outdir, "report.json")
    try:
        s3_get(cfg, f"{key}/create_jobfile.log", log)
    except Exception:
        log = None
    # Job ids come ONLY from the report on S3, never from SSM stdout (capped at 24,000 characters) -- and the
    # report is fetched whatever stdout says, so a lost stdout cannot lose them.
    try:
        s3_get_retry(cfg, f"{key}/report.json", rep)
        with open(rep) as f:
            report = json.load(f)
    except Exception as e:
        tail = open(log, errors="replace").read()[-1500:] if log else "(no log either)"
        files_at = f"this attempt's files: {outdir} and {key}/"
        may = (f"Jobs MAY be queued, and no ledger was written: check squeue and, on the head node, {run}/report.json "
               f"and {run}/create_jobfile.log before resubmitting; `status --batch {batch}` writes the ledger if the "
               f"report reaches S3 ({files_at}).")
        if re.search(r"^JF_ALREADY_LAUNCHED ", out, re.M):
            sys.exit(f"error_id=jobfile_already_launched — {run}/inputs already existed, so another launch of this attempt "
                     f"(a repeated SSM delivery) staged it first and may still be running create_jobfile; this one started "
                     f"nothing. {may}")
        bounded = re.search(r"^JF_LAUNCH_TIMEOUT ", out, re.M)
        if st == "TimedOut" or bounded:
            sys.exit(f"error_id=jobfile_timeout — the launch outran launch_timeout_s={cfg['launch_timeout_s']} and "
                     + ("was stopped by its own bound" if bounded else "SSM gave up on it; its own bound stops it")
                     + f": launch.sh stops create_jobfile, with every sbatch it started, {launch_bound(cfg)} s in, "
                     f"before SSM's timeout. Jobs it queued up to then MAY be queued, and no ledger was written: check "
                     f"squeue and {run}/create_jobfile.log on the head node (one `[submit] <tag> -> job <id>` line per "
                     f"queued job) before resubmitting ({files_at}).\ncreate_jobfile.log tail:\n{tail}")
        up = re.search(r"^JF_REPORT_UPLOAD_FAILED ", out, re.M)
        sys.exit(f"error_id=jobfile_no_report — no readable report reached S3 ({e}; the launch ended {st}"
                 + ("; its report.json upload failed" if up else "") + f"). {may}\ncreate_jobfile.log tail:\n{tail}")

    # A smoke failure or any pre-submit error queued nothing -> no ledger. A partial submit queued real jobs, which
    # must be recorded or they are orphaned.
    queued, ok, jobs, fails = jobfile_result(report)
    why = "".join(f"\n  sbatch {x['tag']} rc={x['submit_rc']}: {x.get('stderr_tail', '')[-200:]}" for x in fails[:10])
    why += f"\n  ... {len(fails) - 10} more in {rep}" if len(fails) > 10 else ""
    if not queued:
        smoke = report.get("smoke") or {}
        eid = "smoke_failed" if report.get("error_id") == "CJ_SMOKE_FAILED" else "jobfile_failed"
        sys.exit(f"error_id={eid} — create_jobfile {report.get('error_id')}: {report.get('message')}. "
                 "No jobs were queued." + why
                 + (f"\n--- smoke output tail ---\n{smoke['output_tail']}" if smoke.get("output_tail") else ""))
    n = report.get("num_pairs")
    led = jobfile_ledger(att, report, ok, jobs, time.time())
    write_json(ledger_path(repo, cfg, batch), led)
    if not ok:
        print(f"[submit] PARTIAL — batch {batch}: {len(jobs)} of {n} jobs queued and recorded; "
              f"{len(fails)} sbatch submission(s) FAILED:{why}")
        sys.exit(f"error_id=submit_partial — {len(fails)} of {n} jobs were NOT queued. The {len(jobs)} that "
                 f"were are in {ledger_path(repo, cfg, batch)}; the failed pairs need a new batch.")
    if len(jobs) != n:
        sys.exit(f"error_id=ledger_incomplete — create_jobfile reported success but returned {len(jobs)} job "
                 f"ids for {n} pairs. The ledger records those {len(jobs)}; check squeue before resubmitting.")
    if n != pairs:
        print(f"[submit] WARNING: create_jobfile counted {n} pairs, the local check {pairs}.")
    print(f"[submit] smoke OK — {led['smoke'].get('trace')}/{led['smoke'].get('exp')} ran clean in "
          f"{led['smoke'].get('elapsed_s')}s on the head node")
    print(f"[submit] OK — batch {batch}: {len(jobs)} jobs queued, running from {run}.")

def recover_attempt(repo, cfg, batch):
    """Read-only recovery of an attempt that froze its batch but has no ledger (killed, interrupted, or its record came
    late): fetch the attempt's own S3 record and, when it proves what was queued, write the ledger. Nothing on the head
    node or in S3 changes. Returns (outcome, note): "recovered", "nothing" (the record says nothing was queued) or
    "unknown"."""
    d = os.path.join(runs_dir(repo, cfg), batch)
    try:
        with open(os.path.join(d, "attempt.json")) as f:
            att = json.load(f)
    except (OSError, ValueError) as e:
        return "unknown", f"its attempt.json is unreadable ({e})"
    gate = f"; its --spot-smoke gate job {att['smoke_job_id']} may be queued too" if att.get("smoke_job_id") else ""
    if att.get("mode") == "jobfile" and all(k in att for k in ("s3", "inputs", "local_inputs")):
        where, rep = f"{att['s3']}/report.json", os.path.join(d, "report.json")
        try:
            s3_get(cfg, where, rep)
            with open(rep) as f:
                report = json.load(f)
        except Exception as e:
            return "unknown", f"no readable report at {where} ({e}); on the head node see {att['remote_run_dir']}/create_jobfile.log"
        queued, ok, jobs, _ = jobfile_result(report)
        if not queued:
            return "nothing", f"its report says no jobs were queued ({report.get('error_id')}: {report.get('message')})"
        led = jobfile_ledger(att, report, ok, jobs, att.get("frozen_at"))
    elif att.get("mode") == "wrapper" and all(k in att for k in ("record", "token", "n_expected")):
        where, local = att["record"], os.path.join(d, "submitted.txt")
        try:
            s3_get(cfg, where, local)
            with open(local) as f:
                jobs, missing = wrapper_record_jobs(f.read(), att["token"])
        except Exception as e:
            jobs, missing = None, f"is not there ({e})"
        if jobs is None:
            return "unknown", f"its job-id record {where} {missing}; on the head node see {att['run_assets']}/submitted.txt{gate}"
        if not jobs:
            return "nothing", f"its job-id record {where} says no jobs were queued{gate}"
        led = wrapper_ledger(att, jobs, att.get("frozen_at"))
    else:
        return "unknown", f"its attempt.json names no per-attempt record to recover from{gate}"
    write_json(ledger_path(repo, cfg, batch), led)
    return "recovered", (f"{len(jobs)} job id(s) from {where}" + (", a partial submit" if led.get("partial") else "")
                         + (f", fewer than the {att['n_expected']} expected" if led.get("incomplete") else ""))

def _attempts_without_ledger(repo, cfg):
    d = runs_dir(repo, cfg)
    return sorted(b for b in (os.listdir(d) if os.path.isdir(d) else [])
                  if os.path.isfile(os.path.join(d, b, "attempt.json")) and not os.path.isfile(os.path.join(d, b, "ledger.json")))

def _report_attempt(repo, cfg, batch):
    outcome, note = recover_attempt(repo, cfg, batch)
    if outcome == "recovered":
        print(f"[status] batch={batch}: recovered its ledger — {note}")
    elif outcome == "nothing":
        print(f"[status] batch={batch} state=failed — {note}; nothing to poll")
    else:
        print(f"[status] batch={batch} state=unknown — may have queued jobs: it froze its batch and has no ledger, and "
              f"{note}. Check squeue before resubmitting.")
    return outcome, note

def verb_status(args):
    repo = args.repo; cfg = load_cfg(repo)
    if args.batch:
        batch = args.batch
        if not os.path.isfile(ledger_path(repo, cfg, batch)):
            if not os.path.isfile(os.path.join(runs_dir(repo, cfg), batch, "attempt.json")):
                sys.exit(f"no batch {batch!r}: {os.path.join(runs_dir(repo, cfg), batch)} holds no ledger or attempt record")
            outcome, note = _report_attempt(repo, cfg, batch)
            if outcome == "unknown":
                sys.exit(f"error_id=batch_unknown — {batch} froze its batch but has no ledger, and {note}. It MAY have "
                         "queued jobs: check squeue before resubmitting, and re-run status once its record may be in S3.")
            if outcome == "nothing":
                return
    else:
        unknown = [b for b in _attempts_without_ledger(repo, cfg) if _report_attempt(repo, cfg, b)[0] == "unknown"]
        batch = _latest_batch(repo, cfg, f"; {len(unknown)} attempt(s) above may have queued jobs" if unknown else "")
    with open(ledger_path(repo, cfg, batch)) as f:
        led = json.load(f)
    head = wake(cfg)
    # ParallelCluster runs Slurm with accounting OFF (no sacct), so detect
    # completion via squeue membership: a job in squeue is active; absent => done.
    st, out, err = ssm_run(cfg, head, [
        f"export PATH=/opt/slurm/bin:$PATH; "
        f"echo QSTART; squeue -h -o '%i %T' 2>/dev/null; echo QEND; "
        f"echo NODES=$(sinfo -h -o '%D %t' | awk '$2==\"mix\"||$2==\"alloc\"||$2==\"idle\"{{s+=$1}}END{{print s+0}}')"],
        timeout=120)
    # QSTART/QEND bracket the queue dump. A missing QEND means SSM truncated the
    # output (24,000-char cap) -- and a truncated queue reads as "those jobs are
    # gone", i.e. finished. Marking a running batch complete would send `collect`
    # after results that do not exist yet, so refuse instead of guessing.
    if "QSTART" not in out or "QEND" not in out:
        sys.exit("error_id=status_truncated — the queue listing came back without its "
                 "end marker, so it cannot be trusted (SSM caps stdout at 24,000 chars). "
                 "Ledger left unchanged; re-run, or query squeue directly.")
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
    write_json(ledger_path(repo, cfg, batch), led)
    nodes = re.search(r"NODES=(\d+)", out)
    print(f"[status] batch={batch} state={led['state']}  " +
          "  ".join(f"{k}={v}" for k, v in sorted(counts.items())) +
          (f"  spot_nodes={nodes.group(1)}" if nodes else ""))

def verb_collect(args):
    repo = args.repo; cfg = load_cfg(repo)
    batch = args.batch
    if not os.path.isfile(ledger_path(repo, cfg, batch)):
        if os.path.isfile(os.path.join(runs_dir(repo, cfg), batch, "attempt.json")):
            sys.exit(f"error_id=batch_unknown — {batch} froze its batch but has no ledger, so it MAY have queued jobs "
                     f"nobody recorded: run `status --batch {batch}`, which recovers the ledger from the attempt's S3 record.")
        sys.exit(f"no batch {batch!r}: {os.path.join(runs_dir(repo, cfg), batch)} holds no ledger")
    with open(ledger_path(repo, cfg, batch)) as f:
        led = json.load(f)
    if led.get("state") != "complete" and not args.force:
        sys.exit("batch not complete (run status; use --force to collect anyway)")
    if led.get("mode") == "jobfile":
        return collect_jobfile(cfg, repo, batch, led)
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

def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def collect_jobfile(cfg, repo, batch, led):
    """Roll a jobfile batch up ON the head node with the batch's own rollup.py and inputs, and bring back
    only stats.csv and rollup_report.json: a raw .out can be tens of MB, and egress is paid."""
    run, names = led["remote_run_dir"], led["inputs"]
    if not SAFE_REMOTE_PATH.match(run) or not all(INPUT_NAME_RE.match(x) for k in ("tlist", "exp", "mfile")
                                                  for x in names[k]):
        sys.exit(f"error_id=bad_ledger — {ledger_path(repo, cfg, batch)} names paths unsafe for a remote shell.")
    U, R, key = cfg["remote_user"], cfg["region"], f"{results_prefix(cfg)}/{batch}"
    flags = " ".join(f"--{k} " + " ".join(f"{run}/inputs/{x}" for x in names[k]) for k in ("mfile", "tlist", "exp"))
    head = wake(cfg); print(f"[collect] head={head} awake; rolling up {run} on the head node...")
    st, out, err = ssm_run(cfg, head, [
        f"runuser -l {U} -c 'python3 {run}/scripts/rollup.py {flags} -d {run} -o {run}/stats.csv "
        f"--report-json {run}/rollup_report.json > {run}/rollup.log 2>&1 || "
        f"{{ echo ROLLUP_FAILED; tail -20 {run}/rollup.log; exit 7; }}; "
        f"aws s3 cp {run}/stats.csv {key}/stats.csv --region {R} --no-progress --only-show-errors && "
        f"aws s3 cp {run}/rollup_report.json {key}/rollup_report.json --region {R} --no-progress "
        f"--only-show-errors && echo ROLLUP_OK'"], timeout=1800)
    if "ROLLUP_OK" not in out:
        sys.exit(f"error_id=rollup_failed — rollup.py or its upload failed on the head node ({st}).\n"
                 f"{out[-1500:]}\n{err[-500:]}")
    outdir = os.path.join(runs_dir(repo, cfg), batch)
    stats, rep = os.path.join(outdir, "stats.csv"), os.path.join(outdir, "rollup_report.json")
    s3_get(cfg, f"{key}/stats.csv", stats)
    s3_get(cfg, f"{key}/rollup_report.json", rep)
    with open(rep) as f:
        report = json.load(f)
    s = report.get("summary") or {}
    print(f"[collect] batch={batch}: rollup {report.get('status')} — total={s.get('total')} "
          f"passed={s.get('passed')} filtered={s.get('filtered')} failed={s.get('failed')} -> {stats}")
    bad = [r for r in report.get("runs") or [] if r.get("status") != "ok"]
    for r in bad[:20]:
        print(f"  {r['status'].upper():9} {r['trace']}/{r['exp']}: {r['error_id']} ({r['reason']})")
    if len(bad) > 20:
        print(f"  ... {len(bad) - 20} more in {rep}")
    with open(stats, newline="") as f:
        rows = list(csv.DictReader(f))
    col = next((c for c in (rows[0] if rows else {}) if c.lower() == "ipc"), None)
    if col is None:
        print("[collect] stats.csv has no `ipc` column; zero-IPC rows were not checked.")
    else:
        # rollup writes 0 both for a failed run and for a stat its .out lacks; only Filter tells them apart
        zero = [r for r in rows if _float(r[col]) == 0]
        if zero:
            print(f"[collect] WARNING: {len(zero)} row(s) have ipc=0 — a failed run (Filter=0) or a stat "
                  "missing from its .out:")
            for r in zero[:20]:
                print(f"  {r.get('TraceName')}/{r.get('ExpName')} Filter={r.get('Filter')}")
            if len(zero) > 20:
                print(f"  ... {len(zero) - 20} more")
    led["collected_at"], led["stats_csv"] = time.time(), stats
    write_json(ledger_path(repo, cfg, batch), led)

def _latest_batch(repo, cfg, note=""):
    d = runs_dir(repo, cfg)
    # a batch's dir exists before its ledger does
    leds = [p for p in (os.path.join(d, b, "ledger.json") for b in (os.listdir(d) if os.path.isdir(d) else []))
            if os.path.isfile(p)]
    if not leds: sys.exit("no batches found" + note)
    return os.path.basename(os.path.dirname(max(leds, key=lambda p: json.load(open(p)).get("submitted_at") or 0)))


def main():
    ap = argparse.ArgumentParser(prog="aws_launch.py")
    sub = ap.add_subparsers(dest="verb", required=True)
    for v in ("configure", "sync", "submit", "status", "collect"):
        s = sub.add_parser(v); s.add_argument("--repo", required=True)
    # per-verb args
    sub.choices["configure"].add_argument("--profile"); sub.choices["configure"].add_argument("--cluster")
    sub.choices["configure"].add_argument("--project", required=True,
        help="OWNER-FIRST '<owner>/<project>', e.g. rbera/hermes-uncore. Namespaces S3 keys "
             "AND the head-node directory. CONFIRM WITH THE HUMAN before using it.")
    sub.choices["configure"].add_argument("--project-root",
        help="default: /home/<remote_user>/<project>")
    sub.choices["configure"].add_argument("--force", action="store_true")
    sub.choices["sync"].add_argument("--build", action="store_true",
        help="also rebuild on the head node after the verified sync")
    sp = sub.choices["submit"]
    sp.add_argument("--no-sync", action="store_true",
        help="skip the source sync and rebuild (the --traces route still builds a missing binary). Only for "
             "adding a batch on an already-synced build; nothing then checks the head node matches your tree.")
    route = sp.add_mutually_exclusive_group(required=True)
    route.add_argument("--traces", help="wrapper route: a file of trace keys, one per line")
    route.add_argument("--tlist", nargs="+", help="jobfile route: create_jobfile trace list YAML(s)")
    sp.add_argument("--spot-smoke", action="store_true",
        help="ALSO gate on a real job run on a Spot compute node (default off). The "
             "head-node gate is mandatory and always runs; this adds coverage of "
             "compute-only surfaces (node IAM profile, the OnNodeConfigured bootstrap "
             "script, /scratch) at the cost of a Spot boot. Worth it on a new or "
             "recently-changed cluster.")
    sp.add_argument("--exps",
        help='wrapper route: semicolon-separated name=knobs; empty knobs -> default_knobs. e.g. "nopref=;pythia=--l2c_prefetcher_types=scooby ..."')
    sp.add_argument("--exp", nargs="+",
        help="jobfile route: experiment YAML(s); each file's $(VAR)s are expanded as create_jobfile does, then "
             "{REMOTE_REPO}/<d> and <remote_repo_path>/<d>, for d in snapshot_dirs, point at the batch's copy")
    sp.add_argument("--mfile", nargs="+", help="jobfile route: metric YAML(s), kept for collect's rollup")
    sp.add_argument("--smoke-idx", type=int,
        help="jobfile route: the (trace x exp) pair index the smoke gate runs (default 0)")
    sp.add_argument("--label")
    sub.choices["status"].add_argument("--batch")
    sub.choices["collect"].add_argument("--batch", required=True)
    sub.choices["collect"].add_argument("--force", action="store_true")
    args = ap.parse_args()
    if args.verb == "submit":
        if args.traces is not None:
            other = [f for f, v in (("--exp", args.exp), ("--mfile", args.mfile), ("--smoke-idx", args.smoke_idx))
                     if v is not None]
            if other:
                sp.error(f"{' '.join(other)} belong to the --tlist route, not --traces")
            if args.exps is None:
                sp.error("--traces needs --exps")
        else:
            other = [f for f, v in (("--exps", args.exps), ("--spot-smoke", args.spot_smoke or None)) if v is not None]
            if other:
                sp.error(f"{' '.join(other)} belong to the --traces route, not --tlist")
            if not (args.exp and args.mfile):
                sp.error("--tlist needs --exp and --mfile")
    {"configure": verb_configure, "sync": verb_sync, "submit": verb_submit,
     "status": verb_status, "collect": verb_collect}[args.verb](args)

if __name__ == "__main__":
    main()
