# skillhub

Canonical home for reusable Claude Code skills across my workflow. Each
subdirectory is one self-contained skill (a `SKILL.md` plus its `reference/`
and `examples/`) packaged as a single-skill plugin, and the repo itself is a
Claude Code plugin marketplace. Installed skills auto-update whenever this
repo is pushed.

## Skills

- **ggplot-house-style** — generate publication-quality charts in a consistent
  house style (ggplot2 + hrbrthemes, dual PNG/PDF export, numbers-CSV per
  chart). Orients to a project's data format once, then plots.
- **git-commit** — prepare a convention-following commit in any repo. Detects the
  repo's commit conventions (message format, formatter, trailers, gates) once,
  persists them to a `.commit-profile`, then formats/verifies/stages/commits.
- **research-report** — write a research-log report for a completed experiment
  campaign, organized like a scientific paper (Key Idea, Mechanism with files +
  commits inventory, Evaluation Methodology, Key Results and Next Steps in
  descending importance). Self-contained figures, title-cased headers.
- **slurm-launch** — launch/monitor/collect batch jobs on an SSH-only Slurm
  cluster via a pluggable per-repo backend (contract: configure/submit/status/
  collect). Generic playbook + operational notes; ChampSim's cluster_run.py is
  the reference backend.

## Install

Add the marketplace, then install the skills you want:

```sh
claude plugin marketplace add git@github.com:rahulbera/skillhub.git
claude plugin install ggplot-house-style@skillhub
claude plugin install git-commit@skillhub
claude plugin install research-report@skillhub
claude plugin install slurm-launch@skillhub
```

Use the **SSH** URL, not `rahulbera/skillhub` over HTTPS. This repo is private,
and the background auto-update pull deliberately disables git credential
helpers, so an HTTPS remote authenticates on manual updates but silently fails
on automatic ones. SSH needs the key loaded in `ssh-agent`, or a
passphrase-less key on disk, plus `github.com` in `known_hosts`.

Auto-update is off by default for third-party marketplaces. Turn it on once,
either through `/plugin` → **Marketplaces** → *skillhub* → **Enable
auto-update**, or by setting it directly in `~/.claude/settings.json`:

```json
{
  "extraKnownMarketplaces": {
    "skillhub": {
      "source": { "source": "git", "url": "git@github.com:rahulbera/skillhub.git" },
      "autoUpdate": true
    }
  },
  "env": {
    "CLAUDE_CODE_PLUGIN_KEEP_MARKETPLACE_ON_FAILURE": "1"
  }
}
```

`CLAUDE_CODE_PLUGIN_KEEP_MARKETPLACE_ON_FAILURE` keeps the existing clone when
a background pull fails, instead of deleting and re-cloning — worth setting for
a private marketplace, where a failed re-clone would otherwise take the skills
offline until the next manual update.

Skills load namespaced by their plugin, e.g. `slurm-launch:slurm-launch`.

## How updates work

No manifest here declares a `version`, deliberately. For a git-hosted
marketplace with relative-path sources, Claude Code falls back to the commit
SHA as the plugin version, so **every push to this repo is an update**.
Declaring a version would pin installs until that string was bumped by hand.

`claude plugin validate --strict` warns about the missing version. Ignore it.

Claude Code refreshes after a session starts, on a random delay of up to ten
minutes, then prompts for `/reload-plugins`. To pull immediately:

```sh
claude plugin marketplace update skillhub
```

## Adding a skill

Each skill is its own plugin, so a new one takes three steps:

1. Create `<name>/SKILL.md` (plus `reference/` and `examples/` as needed). A
   `SKILL.md` at the plugin root with no `skills/` subdirectory is discovered
   automatically as a single-skill plugin.
2. Add `<name>/.claude-plugin/plugin.json` and an entry in
   `.claude-plugin/marketplace.json` with `"source": "./<name>"`. Copy an
   existing pair; omit `version` in both.
3. Push, then `claude plugin install <name>@skillhub` once on each machine.

Edits to skills that are already installed need none of this — just push.

Validate before pushing:

```sh
claude plugin validate .
```

## Vendoring into a repo

Some skills carry a `sync.sh` that copies them into a consuming repo under
`.claude/skills/<name>/`. That path is no longer how these skills reach my own
machines — it is for shipping a skill to collaborators or CI, where a private
marketplace is not reachable.
