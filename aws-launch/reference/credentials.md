# Credentials — one-time setup (new cluster users start here)

`aws-launch` never stores AWS keys. All access goes through a **named AWS profile**
you set up once per machine; the skill only ever uses the profile's *name*. Keys stay
in `~/.aws/` and out of the repo, the config, and chat.

## Which identity you have
The shared `champsim` cluster (account `524558748675`, region `us-east-1`) has two
kinds of identity:

- **Cluster users (almost everyone).** You get a personal IAM user,
  `champsim-<name>`, whose access key can do exactly one thing: assume your personal
  role, `ChampSimRunner<Name>`. That role can read the traces, read and write
  **only** `s3://champsim-results-all/<name>/…`, wake the head node, and submit and
  inspect jobs on it. It cannot stop or terminate instances, change the cluster,
  touch IAM or budgets, or write anyone else's results.
- **Cluster owner.** Creates and updates the cluster itself by assuming
  `ParallelClusterDeployer`. If that is not you, you do not need it — skip to setup.

## What your admin gives you
1. Your key file, `champsim-<name>-key.json`, holding an `AccessKeyId` and a
   `SecretAccessKey`. It is shown once, when created; nobody else keeps a copy.
2. Your role ARN: `arn:aws:iam::524558748675:role/parallelcluster/ChampSimRunner<Name>`
   (for Mihai: `…/ChampSimRunnerMihai`).

## Setup
1. Install **AWS CLI v2** (v1 handles assumed roles differently), **Python 3** with
   **PyYAML** (`pip install pyyaml`), and Claude Code.

2. Put your key in `~/.aws/credentials`, copying the two values from your key file:
   ```ini
   [champsim-key]
   aws_access_key_id = AKIA...
   aws_secret_access_key = ...
   ```

3. Add the profile that assumes your role to `~/.aws/config`:
   ```ini
   [profile champsim]
   role_arn = arn:aws:iam::524558748675:role/parallelcluster/ChampSimRunner<Name>
   source_profile = champsim-key
   region = us-east-1
   duration_seconds = 3600
   ```
   Then lock both files down: `chmod 700 ~/.aws && chmod 600 ~/.aws/*`.

4. Verify:
   ```bash
   aws sts get-caller-identity --profile champsim
   # Arn must contain  assumed-role/ChampSimRunner<Name>/
   aws s3 ls s3://champsim-traces-all/ --profile champsim
   # lists trace prefixes such as version2/ and version2.1/
   ```

5. When the skill bootstraps a repo (`configure`), give it:
   - profile **`champsim`** (or whatever you named the profile in step 3)
   - cluster **`champsim`**
   - project **`<name>/<project>`** — the first part MUST be your own name, in
     lowercase, e.g. `mihai/pythia-sweep`. Your role can only write under
     `champsim-results-all/<name>/`, so any other owner makes every upload fail
     with `AccessDenied`.

That's it. There is no MFA prompt and nothing to renew: the CLI trades your key for a
fresh 1-hour role session on its own whenever the last one expires. Long simulations
are unaffected either way — jobs run on the cluster under the cluster's own
permissions, not your session.

## Keep the key safe
Your access key is the only thing standing between the internet and your share of the
cluster. Keep it only in `~/.aws/credentials`: never in a repo, `config.yml`, a
script, or a chat. If it may have leaked, tell your admin; they delete it and issue a
new one (`cluster-access-setup.sh --reissue-keys=<Name>`).

## When the setup fails
| Symptom | Cause and fix |
|---|---|
| `InvalidClientTokenId` or `SignatureDoesNotMatch` | The key in `[champsim-key]` is mistyped, or was deleted. Re-copy both values from the key file, or ask your admin to reissue it. |
| `AccessDenied` when calling the `AssumeRole` operation | Wrong `role_arn` (check the name's capitalization), or your user was never allowed to assume it. The admin fixes the latter by running `cluster-access-setup.sh --people=<Name>`. |
| `get-caller-identity` shows `user/champsim-<name>`, not `assumed-role/…` | You ran it with `--profile champsim-key` (the raw key) instead of `--profile champsim`. |
| `AccessDenied` on `PutObject` / uploads fail | The owner part of `project` in `config.yml` is not your lowercase name. |
| `AccessDenied` on stopping or terminating instances, CloudFormation, IAM or budgets | Intentional; runner roles cannot change the cluster. Ask the cluster owner. |
| `ExpiredToken` in the middle of a command | The 1-hour session rolled over; retry. |
