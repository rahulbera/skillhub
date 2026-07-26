# skillhub

Canonical home for reusable Claude Code skills across my workflow. Each
subdirectory is one self-contained skill (a `SKILL.md` plus its `reference/`
and `examples/`). Skills are synced into consuming repos under
`.claude/skills/<name>/` via each skill's `sync.sh`.

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
