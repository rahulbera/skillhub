# Credentials — one-time setup (interns & collaborators start here)

`aws-launch` never stores AWS keys. All access goes through a **named AWS
profile** you configure once on each machine; the skill just uses it. This keeps
onboarding to a single paste and keeps secrets out of the repo, config, and shell
history.

## What you need from your admin
An AWS identity that can **assume the cluster's deployer role**
(`ParallelClusterDeployer`). Your admin gives you **one** of:
- an **access key + secret** (an IAM user that can assume the role), or
- **SSO** access (an AWS IAM Identity Center login), or
- a ready-made `~/.aws/config` + `~/.aws/credentials` snippet to drop in.

You do **not** need to understand AWS to use the cluster — just complete one of
the setups below, then hand the skill your profile name.

## Option A — access key that assumes the role (most common)
Put the key in `~/.aws/credentials`:
```ini
[aws-launch-src]
aws_access_key_id = AKIA...
aws_secret_access_key = ...
```
and the role assumption in `~/.aws/config`:
```ini
[profile aws-launch]
role_arn = arn:aws:iam::<ACCOUNT_ID>:role/ParallelClusterDeployer
source_profile = aws-launch-src
region = us-east-1
duration_seconds = 3600
```
Lock it down: `chmod 700 ~/.aws && chmod 600 ~/.aws/*`.

## Option B — plain access key (if your key IS the deployer identity)
```bash
aws configure --profile aws-launch      # paste key, secret; region us-east-1
```

## Option C — SSO
```bash
aws configure sso --profile aws-launch  # follow the browser login
```

## Verify (the skill does this at bootstrap, but you can too)
```bash
AWS_PROFILE=aws-launch aws sts get-caller-identity
# the Arn should contain assumed-role/ParallelClusterDeployer
```
Then set `aws_profile: aws-launch` in `<repo>/.aws-launch/config.yml` (bootstrap
writes it for you). That's it — `submit`/`status`/`collect` now work.

## Notes
- **Never commit keys or paste them into config.yml / chat.** They live only in
  `~/.aws/`. The skill and config reference the profile *name* only.
- The cluster is a **shared** resource — everyone submits to the same Spot queue,
  and the head-node wake/auto-stop is shared and idempotent (whoever submits next
  transparently wakes it). Your admin provisions each person's IAM identity; the
  skill is agnostic to how you authenticate.
- STS tokens are short-lived (e.g. 1 h) and auto-refresh from the source profile —
  no action needed if a long run outlasts a token.
- If a call fails with `AccessDenied` on a service-linked role or budgets, that's
  an admin-side account setup item, not your profile — see
  `operational-notes.md` (§ "Why a command failed").
