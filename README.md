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

Add the marketplace once per machine:

```sh
claude plugin marketplace add https://github.com/rahulbera/skillhub.git
```

Prefer that HTTPS URL over the `rahulbera/skillhub` shorthand, which clones
over SSH and so needs a GitHub key on every machine that uses it.

Then install per project, from inside the repo:

```sh
cd /path/to/repo
claude plugin install ggplot-house-style@skillhub --scope project
claude plugin install git-commit@skillhub        --scope project
claude plugin install research-report@skillhub   --scope project
claude plugin install slurm-launch@skillhub      --scope project
```

Run `/reload-plugins` afterwards in any open session. Skills load namespaced
by their plugin, e.g. `slurm-launch:slurm-launch`.

## Scope

Install at **project** scope. `--scope project` records the choice in that
repo's `.claude/settings.json`, so each repo takes only the skills it wants —
which matters because skills vary per project. A repo that keeps its own
**specialized fork** of a skill (gem5 has a `git-commit` built around gem5's
tag-based header format and its own pre-commit gate) must simply not install
the marketplace version there; installing it would shadow the fork with the
generic one.

Commit the resulting `.claude/settings.json` and collaborators inherit the
choice. They still run `claude plugin install` themselves — since Claude Code
v2.1.195, a plugin from an external source is never auto-installed by a
project's settings alone. To hand them the catalog too:

```sh
claude plugin marketplace add https://github.com/rahulbera/skillhub.git --scope project
```

User scope (the default, no `--scope`) installs a skill into *every* project on
that machine. Reserve it for skills that are genuinely universal.

Per-repo *conventions* are a different axis from scope: `git-commit` adapts to
a repo through its `.commit-profile`, so differing commit formats alone are not
a reason to fork the skill.

## How updates work

No manifest here declares a `version`, deliberately. For a git-hosted
marketplace with relative-path sources, Claude Code falls back to the commit
SHA as the plugin version, so **every push to this repo is an update**.
Declaring a version would pin installs until that string was bumped by hand.

`claude plugin validate --strict` warns about the missing version. Ignore it.

Auto-update is off by default for third-party marketplaces. Turn it on through
`/plugin` → **Marketplaces** → *skillhub* → **Enable auto-update**, or set it
directly in `~/.claude/settings.json`:

```json
{
  "extraKnownMarketplaces": {
    "skillhub": {
      "source": { "source": "git", "url": "https://github.com/rahulbera/skillhub.git" },
      "autoUpdate": true
    }
  }
}
```

Claude Code then refreshes after a session starts, on a random delay of up to
ten minutes, and prompts for `/reload-plugins`. To pull immediately:

```sh
claude plugin marketplace update skillhub
```

Setting `CLAUDE_CODE_PLUGIN_KEEP_MARKETPLACE_ON_FAILURE=1` in `env` keeps the
existing clone when a background pull fails, instead of deleting and
re-cloning. Optional, but it stops a network blip from taking the skills
offline until the next successful update.

## Adding a skill

Each skill is its own plugin, so a new one takes three steps:

1. Create `<name>/SKILL.md` (plus `reference/` and `examples/` as needed). A
   `SKILL.md` at the plugin root with no `skills/` subdirectory is discovered
   automatically as a single-skill plugin.
2. Add `<name>/.claude-plugin/plugin.json` and an entry in
   `.claude-plugin/marketplace.json` with `"source": "./<name>"`. Copy an
   existing pair; omit `version` in both.
3. Push, then `claude plugin install <name>@skillhub --scope project` in each
   repo that wants it.

Edits to skills that are already installed need none of this — just push.

Validate before pushing:

```sh
claude plugin validate .
```

## Vendoring into a repo

Some skills carry a `sync.sh` that copies them into a consuming repo under
`.claude/skills/<name>/`. That predates the marketplace and is no longer how
these skills are distributed — a vendored copy is a snapshot that never
updates. Keep it only for environments that cannot reach the marketplace, and
never `sync.sh` over a repo that maintains its own fork of a skill.
